"""HTTP surface for the sprint-recovery durable workflow (app/sprint_recovery/graph.py). Separate
router file, included into app.main's app alongside app.api.routes' router — that file is already 700+
lines and this feature has enough endpoints (7) to warrant its own module rather than growing it further.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.routes import _authorize_space_ids, _authorize_workflow_state, _caller_identity
from app.observability import SPRINT_RECOVERY_INDEX_CATCH_UP_TIMEOUTS_TOTAL
from app.sprint_recovery.active_threads import find_active_thread_id, register_active_thread
from app.sprint_recovery.graph import (
    ON_STAGE_VAR,
    _describe_action,
    build_escalation_summary,
    flatten_escalation_summary,
    list_checkpoint_history,
    retry_recovery_commit,
    time_travel_resume,
    trigger_reevaluation,
)
from app.sprint_recovery.notifications import notify_escalation
from app.sprint_recovery.schemas import RecoveryAction
from app.sprint_recovery.state import initial_recovery_state

router = APIRouter(prefix="/sprint-recovery")
logger = logging.getLogger(__name__)


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _graph(request: Request):
    graph = getattr(request.app.state, "sprint_recovery_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="sprint recovery is disabled (AI_SPRINT_RECOVERY_ENABLED=false)")
    return graph


async def _register_active_thread_if_configured(request: Request, space_id: int, sprint_id: int, thread_id: str) -> None:
    """Best-effort: a failure here must never break starting a real check over a side table that only
    exists to make *finding* it again more convenient. Registered before the graph even runs its first
    diagnosis, deliberately — see active_threads.py's own docstring on why early registration, not just
    convenient, matters for crash-resume to actually be discoverable."""
    db_url = getattr(request.app.state, "sprint_recovery_checkpoint_db_url", None)
    if not db_url:
        return
    try:
        await register_active_thread(db_url, space_id, sprint_id, thread_id)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("failed to register active sprint-recovery thread (space=%s, sprint=%s)", space_id, sprint_id)


def _register_if_waiting(request: Request, sprint_id: int, thread_id: str, values: dict) -> None:
    """Tells the Kafka trigger consumer (if running) to watch for events relevant to this sprint the
    moment a workflow actually reaches `wait_for_reevaluation` — see kafka_trigger.py's own docstring
    for why this in-process registration doesn't survive an ai-service restart, a known, named gap.
    """
    if values.get("status") != "waiting_reevaluation":
        return
    trigger = getattr(request.app.state, "sprint_recovery_kafka_trigger", None)
    if trigger is not None:
        tracked = {s.issue_key for s in (values.get("risk_signals") or []) if s.issue_key}
        plan = values.get("approved_plan")
        if plan is not None:
            for action in plan.actions:
                tracked.add(action.target_issue_key)
                if action.depends_on_issue_key:
                    tracked.add(action.depends_on_issue_key)
        trigger.register_waiting(values["space_id"], sprint_id, thread_id, tracked)


async def _auto_reevaluate_after_commit(graph, thread_id: str, values: dict) -> dict:
    """If approving a plan just finished committing all its actions (status now
    `waiting_reevaluation`), immediately re-check once instead of leaving the human staring at a
    "Re-check now" button for a state the system could answer itself in the same request.

    **Found live, from direct product feedback**: `waiting_reevaluation` is a real, intentional pause
    — the whole point of `wait_for_reevaluation_node` is that it's resumable by either a human click
    or a real Kafka event, and that dual-resume behavior is worth keeping and demoing on its own. But
    making the human always be the one to notice and click, when the very actions they just approved
    are what the Kafka event would react to anyway, adds a network round-trip's worth of dead air for
    no benefit — "I approved a plan, now tell me what happened" is one action from the user's point of
    view, not two. This chains exactly one `trigger_reevaluation` call, synchronously, in the same
    request. `reevaluate_node` always resolves to `recovered`, `escalated`, or a fresh
    `awaiting_plan_approval` — never back to `waiting_reevaluation` — so this cannot loop.

    Reading a stale sprint here (the index hasn't caught up with the writes just committed) is a real
    hazard this route used to guard against with a fixed 2-second sleep. That guess is gone:
    `reevaluate_node` now waits on a recorded per-issue watermark instead, which covers *every* resume
    path rather than just this one — see `_await_index_catch_up` in `app/sprint_recovery/graph.py`.
    """
    if values.get("status") != "waiting_reevaluation":
        return values
    return await trigger_reevaluation(graph, thread_id, source="auto_after_approve")


async def _notify_if_escalated(request: Request, thread_id: str, sprint_id: int, values: dict) -> None:
    """The other half of `_register_if_waiting`'s "react to the status *after* the fact" shape — fires
    exactly once, right when a `trigger_reevaluation` call's result newly shows status=="escalated",
    never on a plain `GET` poll (which would spam a notification on every page reload). See
    `app/sprint_recovery/notifications.py` for what "notify" actually does.
    """
    if values.get("status") != "escalated":
        return
    graph = _graph(request)
    settings = request.app.state.settings
    summary = await build_escalation_summary(graph, thread_id)
    await notify_escalation(
        settings.sprint_recovery_escalation_webhook_url, thread_id, values["space_id"], sprint_id,
        values["sprint_name"], flatten_escalation_summary(summary),
    )


def _track_index_lag_if_timed_out(values: dict) -> None:
    """Same "react from outside the node, once per actual reevaluation, not per poll" shape as
    `_notify_if_escalated` — and the same reason: `reevaluate_node` itself isn't the safe place for this
    increment, since a crash-and-resume could re-run the node and double-count. Aggregate counter, not
    per-request messaging: `RecoveryStatusResponse.index_catch_up_timed_out` already tells one caller
    about one check; this is what an on-call would actually watch to tell "one unlucky request" apart
    from "the Kafka/vectorization-service pipeline is genuinely degraded."
    """
    if not values.get("index_catch_up_timed_out"):
        return
    SPRINT_RECOVERY_INDEX_CATCH_UP_TIMEOUTS_TOTAL.labels(str(values["space_id"])).inc()
    logger.warning(
        "sprint recovery reevaluation timed out waiting for the read model to catch up — reading anyway",
        extra={"space_id": values["space_id"], "sprint_id": values["sprint_id"]},
    )


class ActionOut(_CamelModel):
    action_type: str
    target_issue_key: str
    depends_on_issue_key: Optional[str]
    new_priority: Optional[str]
    comment_body: Optional[str]


class PlanOut(_CamelModel):
    plan_id: str
    name: str
    rationale: str
    impact_on_goal: str
    actions: List[ActionOut]


class HypothesisOut(_CamelModel):
    statement: str
    confidence: str
    supporting_evidence_ids: List[str]


class EvidenceOut(_CamelModel):
    citation_id: str
    issue_key: str
    source_type: str
    content: str


class EscalationRoundOut(_CamelModel):
    round: int
    plan_name: str
    rationale: str
    actions: List[str]


class RecoveryStatusResponse(_CamelModel):
    thread_id: str
    status: str
    risk_signal_count: int
    evidence: List[EvidenceOut]
    hypotheses: List[HypothesisOut]
    clarification_question: Optional[str]
    plans: List[PlanOut]
    committed_actions: Dict[str, str]
    escalation_round: int
    plan_revision_round: int
    # Exposed so the frontend can proactively disable "request different plans" once the cap is hit,
    # instead of a human submitting one more revise request that silently degrades to a plain reject
    # inside approval_node — found live: a real revise-past-the-cap looked indistinguishable from a
    # normal rejection in the UI before this field existed.
    max_plan_revision_rounds: int
    # Lets the UI say "Attempt 2 of 3" instead of a bare, meaningless "escalation round 1" — a reader
    # can't tell how much runway is left from a counter with no denominator.
    max_escalation_rounds: int
    token_usage: int
    error: Optional[str]
    # Only ever populated when status=="escalated" — see build_escalation_summary's docstring.
    # Structured (a list of per-round cards + a separate reasons list), not a pre-flattened paragraph —
    # found live that one run-on paragraph covering 3 rounds was unreadable in the UI.
    escalation_rounds: List[EscalationRoundOut] = Field(default_factory=list)
    escalation_still_at_risk_reasons: List[str] = Field(default_factory=list)
    # Distinguishes two very different things that both land on status=="escalated" — see
    # build_escalation_summary's docstring. Empty: every flagged issue was acted on at least once
    # across the rounds above, so what remains is real engineering time, not more planning. Non-empty:
    # these issue keys were never acted on at all (e.g. the token budget ran out first) — a real gap.
    escalation_unaddressed_issue_keys: List[str] = Field(default_factory=list)
    # True only on a status this workflow reached without confirming the read model had caught up with
    # its own writes first (see `_await_index_catch_up`). Staleness can only ever bias a result toward
    # looking *more* at-risk than reality (see the field's own comment in state.py) — surfaced so an
    # "escalated"/"diagnosing" result right after actions were just approved doesn't read as an
    # unexplained inconsistency. Never true on a fresh "diagnosing" that hasn't reevaluated anything yet.
    index_catch_up_timed_out: bool = False
    # **Found live, from a direct user question ("can the UI even show which action is left after a
    # crash?")**: `plans` is always `state["plans"]` — the *originally proposed* 1-3 options from
    # `plan_node`, never touched again after approval. `decision="edit"` replaces the actual executing
    # plan with a human-modified `RecoveryPlan` (`approved_plan`), but the API never exposed that object
    # — only the pre-edit proposal. Reproduced live: edited a plan to target a nonexistent issue key,
    # approved it, watched it fail partway through — the response's `plans` still showed the original,
    # un-edited action list, not what had actually been sent to Jira or what `committed_actions`' indices
    # actually refer to. Checkmarks and action text would have been wrong, not just stale, whenever an
    # edit changed the action count/order/content. Populated once a plan is actually approved (covers
    # committing/failed/waiting_reevaluation/recovered/escalated); `None` while still choosing.
    approved_plan: Optional[PlanOut] = None


def _status_response(thread_id: str, values: dict) -> RecoveryStatusResponse:
    return RecoveryStatusResponse(
        thread_id=thread_id,
        status=values.get("status", "diagnosing"),
        risk_signal_count=len(values.get("risk_signals") or []),
        evidence=[EvidenceOut(**e.model_dump()) for e in values.get("evidence") or []],
        hypotheses=[HypothesisOut(**h.model_dump()) for h in values.get("hypotheses") or []],
        clarification_question=values.get("clarification_question"),
        plans=[PlanOut(**p.model_dump()) for p in values.get("plans") or []],
        committed_actions=values.get("committed_actions") or {},
        escalation_round=values.get("escalation_round", 0),
        plan_revision_round=values.get("plan_revision_round", 0),
        max_plan_revision_rounds=values.get("max_plan_revision_rounds", 2),
        max_escalation_rounds=values.get("max_escalation_rounds", 2),
        token_usage=values.get("token_usage", 0),
        error=values.get("error"),
        index_catch_up_timed_out=values.get("index_catch_up_timed_out", False),
        approved_plan=PlanOut(**values["approved_plan"].model_dump()) if values.get("approved_plan") else None,
    )


async def _status_response_with_summary(thread_id: str, values: dict, graph) -> RecoveryStatusResponse:
    """`_status_response` plus the structured round-by-round summary (what was actually committed to
    Jira) when status=="escalated" — separate from that sync function because computing the summary
    needs an awaited `aget_state_history` walk. Safe to call on every status, not just the moment of
    transition: unlike `_notify_if_escalated` (fires once, only on a real transition), this is
    read-only and just makes "escalated" viewable correctly whenever this thread's status is fetched —
    including reopening the modal long after the transition happened.
    """
    response = _status_response(thread_id, values)
    if response.status == "escalated":
        summary = await build_escalation_summary(graph, thread_id)
        response.escalation_rounds = [EscalationRoundOut(**r) for r in summary["rounds"]]
        response.escalation_still_at_risk_reasons = summary["still_at_risk_reasons"]
        response.escalation_unaddressed_issue_keys = summary["unaddressed_issue_keys"]
    return response


def _sse(event_name: str, data: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"


async def _stream_call(coro) -> AsyncIterator[str]:
    """Shared SSE progress-event generator for a single `graph.ainvoke`/resume call — same
    queue-draining shape as `POST /ask/stream` (app/api/routes.py's `ask_stream`), reused here via
    `ON_STAGE_VAR` instead of an `on_stage` kwarg because the graph is one shared, long-lived instance
    built at startup (see that ContextVar's docstring in `sprint_recovery/graph.py` for why). Yields
    `stage` SSE frames as `diagnose_node`/`plan_node` reach them; does NOT yield a `result`/`error`
    frame itself — the caller owns that, since only it knows the request-specific response shape (a
    fresh status vs. a resumed one) and whether to run `_register_if_waiting` afterward.
    """
    stage_queue: asyncio.Queue[str] = asyncio.Queue()
    token = ON_STAGE_VAR.set(stage_queue.put_nowait)
    try:
        task = asyncio.create_task(coro)
        while not task.done():
            try:
                label = await asyncio.wait_for(stage_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            yield _sse("stage", {"label": label})
        while not stage_queue.empty():
            yield _sse("stage", {"label": stage_queue.get_nowait()})
        task.result()  # re-raises whatever graph.ainvoke raised, for the caller's try/except to surface
    finally:
        ON_STAGE_VAR.reset(token)


class StartRecoveryRequest(_CamelModel):
    space_id: int
    sprint_id: int
    sprint_name: str = Field(min_length=1)


@router.post("/start", response_model=RecoveryStatusResponse)
async def start_recovery_endpoint(req: StartRecoveryRequest, request: Request) -> RecoveryStatusResponse:
    await _authorize_space_ids(request, [req.space_id])
    graph = _graph(request)
    user_id, username = _caller_identity(request)
    thread_id = str(uuid.uuid4())
    await _register_active_thread_if_configured(request, req.space_id, req.sprint_id, thread_id)
    settings = request.app.state.settings
    initial = initial_recovery_state(
        req.space_id, req.sprint_id, req.sprint_name, user_id, username,
        max_clarification_rounds=settings.sprint_recovery_max_clarification_rounds,
        max_escalation_rounds=settings.sprint_recovery_max_escalation_rounds,
        max_token_budget=settings.sprint_recovery_max_token_budget,
        max_plan_revision_rounds=settings.sprint_recovery_max_plan_revision_rounds,
    )
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(initial, config=config)
    snap = await graph.aget_state(config)
    return _status_response(thread_id, snap.values)


@router.post("/start/stream")
async def start_recovery_stream_endpoint(req: StartRecoveryRequest, request: Request) -> StreamingResponse:
    """SSE variant of POST /start — identical request/response shape, `stage` events fire as
    `diagnose_node`/`plan_node` (the two real LLM calls a fresh diagnosis chains through in one call)
    are reached, instead of the caller seeing nothing for however long both take. See `_stream_call`.
    """
    await _authorize_space_ids(request, [req.space_id])
    graph = _graph(request)
    user_id, username = _caller_identity(request)
    thread_id = str(uuid.uuid4())
    await _register_active_thread_if_configured(request, req.space_id, req.sprint_id, thread_id)
    settings = request.app.state.settings
    initial = initial_recovery_state(
        req.space_id, req.sprint_id, req.sprint_name, user_id, username,
        max_clarification_rounds=settings.sprint_recovery_max_clarification_rounds,
        max_escalation_rounds=settings.sprint_recovery_max_escalation_rounds,
        max_token_budget=settings.sprint_recovery_max_token_budget,
        max_plan_revision_rounds=settings.sprint_recovery_max_plan_revision_rounds,
    )
    config = {"configurable": {"thread_id": thread_id}}

    async def generate() -> AsyncIterator[str]:
        try:
            async for event in _stream_call(graph.ainvoke(initial, config=config)):
                yield event
        except Exception as exc:  # noqa: BLE001 - clean SSE error frame, same as ask_stream's backstop
            logger.exception("sprint recovery start stream failed")
            yield _sse("error", {"detail": "sprint recovery request failed"})
            return
        snap = await graph.aget_state(config)
        yield _sse("result", _status_response(thread_id, snap.values).model_dump(by_alias=True))

    return StreamingResponse(generate(), media_type="text/event-stream")


# Statuses a caller should never be silently resumed into — there's nothing left to do, so `/by-sprint`
# treats these the same as "no active thread" rather than forcing every reopen to stare at old history.
# `_status_response_with_summary`'s escalation-summary/history views remain reachable directly by
# thread_id for anyone who wants to review a finished run — this only gates the *automatic* resume.
_TERMINAL_STATUSES = {"recovered", "escalated", "rejected", "no_risk_found"}


@router.get("/by-sprint", response_model=Optional[RecoveryStatusResponse])
async def find_active_recovery_endpoint(space_id: int, sprint_id: int, request: Request) -> Optional[RecoveryStatusResponse]:
    """**Found live, from a direct product question**: crash-resume was verified correct at the graph
    level (same thread_id, `/retry` picks up exactly where a killed process left off) — but the
    frontend had no way to *discover* that thread_id after losing it from browser memory (modal closed,
    page reloaded, or the crash itself). `handleStart` always called `POST /start`, which always mints
    a fresh thread; every other endpoint requires already knowing thread_id. The checkpoint was never
    actually lost, nothing could find it again. Called on mount, before offering "Analyze Sprint
    Health" — if this finds a non-terminal thread, the frontend resumes showing it directly instead of
    starting over. Registered in `active_threads.py`, not in `state["risk_signals"]`/etc: the
    checkpointer's own tables have no `space_id`/`sprint_id` columns to query by.
    """
    await _authorize_space_ids(request, [space_id])
    db_url = getattr(request.app.state, "sprint_recovery_checkpoint_db_url", None)
    if not db_url:
        return None
    thread_id = await find_active_thread_id(db_url, space_id, sprint_id)
    if not thread_id:
        return None
    graph = _graph(request)
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not snap.values or snap.values.get("status") in _TERMINAL_STATUSES:
        return None
    return await _status_response_with_summary(thread_id, snap.values, graph)


@router.get("/{thread_id}", response_model=RecoveryStatusResponse)
async def get_recovery_status_endpoint(thread_id: str, request: Request) -> RecoveryStatusResponse:
    graph = _graph(request)
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    return await _status_response_with_summary(thread_id, snap.values, graph)


class ClarifyRequest(_CamelModel):
    answer: str = Field(min_length=1)


@router.post("/{thread_id}/clarify", response_model=RecoveryStatusResponse)
async def clarify_recovery_endpoint(thread_id: str, req: ClarifyRequest, request: Request) -> RecoveryStatusResponse:
    graph = _graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    if not snap.values.get("clarification_question"):
        raise HTTPException(status_code=409, detail=f"rollout {thread_id} is not awaiting clarification")
    await graph.ainvoke(Command(resume={"answer": req.answer}), config=config)
    final = await graph.aget_state(config)
    _register_if_waiting(request, snap.values["sprint_id"], thread_id, final.values)
    return _status_response(thread_id, final.values)


@router.post("/{thread_id}/clarify/stream")
async def clarify_recovery_stream_endpoint(thread_id: str, req: ClarifyRequest, request: Request) -> StreamingResponse:
    """SSE variant of POST /{thread_id}/clarify — answering folds back into `diagnose_node`, which on
    a confident second pass chains straight into `plan_node` too, so this can carry 1-2 more LLM calls
    behind one HTTP request exactly like /start does. See `_stream_call`.
    """
    graph = _graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    if not snap.values.get("clarification_question"):
        raise HTTPException(status_code=409, detail=f"rollout {thread_id} is not awaiting clarification")
    sprint_id = snap.values["sprint_id"]

    async def generate() -> AsyncIterator[str]:
        try:
            async for event in _stream_call(graph.ainvoke(Command(resume={"answer": req.answer}), config=config)):
                yield event
        except Exception as exc:  # noqa: BLE001 - clean SSE error frame, same as ask_stream's backstop
            logger.exception("sprint recovery clarification stream failed")
            yield _sse("error", {"detail": "sprint recovery request failed"})
            return
        final = await graph.aget_state(config)
        _register_if_waiting(request, sprint_id, thread_id, final.values)
        yield _sse("result", _status_response(thread_id, final.values).model_dump(by_alias=True))

    return StreamingResponse(generate(), media_type="text/event-stream")


class ActionIn(_CamelModel):
    action_type: Literal["link_dependency", "change_priority", "move_out_of_sprint", "add_comment"]
    target_issue_key: str
    depends_on_issue_key: Optional[str] = None
    new_priority: Optional[str] = None
    comment_body: Optional[str] = None


class DecisionRequest(_CamelModel):
    decision: Literal["approve", "edit", "reject", "revise"]
    # Required for approve/edit (which specific plan), meaningless for reject/revise (there is no
    # single plan being acted on — all offered plans are being turned down, either final (reject) or
    # with feedback for a new round (revise)).
    plan_id: Optional[str] = None
    actions: Optional[List[ActionIn]] = None
    # Required for revise: what a human wants changed, folded into plan_node's next prompt verbatim —
    # see approval_node's docstring for why this loops back into "plan" instead of ending the workflow.
    feedback: Optional[str] = None


def _decision_resume_payload(req: DecisionRequest) -> dict:
    """Shared validation + `Command(resume=...)` payload builder for both the plain and SSE decision
    endpoints below — factored out (unlike /start's duplicated setup) because this one actually branches
    on business rules per decision type, not just boilerplate construction worth repeating.
    """
    if req.decision in ("approve", "edit") and not req.plan_id:
        raise HTTPException(status_code=422, detail=f"decision={req.decision} requires plan_id")
    if req.decision == "edit" and not req.actions:
        raise HTTPException(status_code=422, detail="decision=edit requires actions")
    if req.decision == "revise" and not (req.feedback and req.feedback.strip()):
        raise HTTPException(status_code=422, detail="decision=revise requires feedback")

    resume_payload: dict = {"decision": req.decision}
    if req.plan_id:
        resume_payload["plan_id"] = req.plan_id
    if req.decision == "edit":
        resume_payload["actions"] = [a.model_dump() for a in req.actions]
    if req.decision == "revise":
        resume_payload["feedback"] = req.feedback
    return resume_payload


def _require_awaiting_approval(thread_id: str, snap_values: dict) -> None:
    if not snap_values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    if snap_values.get("status") != "awaiting_plan_approval":
        raise HTTPException(
            status_code=409,
            detail=f"sprint-recovery {thread_id} is not awaiting plan approval (status={snap_values.get('status')})",
        )


@router.post("/{thread_id}/decision", response_model=RecoveryStatusResponse)
async def submit_recovery_decision_endpoint(
    thread_id: str, req: DecisionRequest, request: Request
) -> RecoveryStatusResponse:
    graph = _graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    _require_awaiting_approval(thread_id, snap.values)
    resume_payload = _decision_resume_payload(req)

    await graph.ainvoke(Command(resume=resume_payload), config=config)
    final = await graph.aget_state(config)
    final_values = await _auto_reevaluate_after_commit(graph, thread_id, final.values)
    _register_if_waiting(request, snap.values["sprint_id"], thread_id, final_values)
    await _notify_if_escalated(request, thread_id, snap.values["sprint_id"], final_values)
    _track_index_lag_if_timed_out(final_values)
    return await _status_response_with_summary(thread_id, final_values, graph)


@router.post("/{thread_id}/decision/stream")
async def submit_recovery_decision_stream_endpoint(
    thread_id: str, req: DecisionRequest, request: Request
) -> StreamingResponse:
    """SSE variant of POST /{thread_id}/decision. `decision="revise"` makes a real LLM call re-entering
    `plan_node`; `decision="approve"` can too, once indirectly — if committing finishes and
    `_auto_reevaluate_after_commit` immediately re-checks, a still-at-risk sprint routes straight into
    `plan_node` again in the same request (see that function's docstring). Both phases stream through
    the same connection so a caller watching stage events sees the whole thing, not just the first half.
    """
    graph = _graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    _require_awaiting_approval(thread_id, snap.values)
    resume_payload = _decision_resume_payload(req)
    sprint_id = snap.values["sprint_id"]

    async def generate() -> AsyncIterator[str]:
        try:
            async for event in _stream_call(graph.ainvoke(Command(resume=resume_payload), config=config)):
                yield event
            final = await graph.aget_state(config)
            if final.values.get("status") == "waiting_reevaluation":
                async for event in _stream_call(
                    trigger_reevaluation(graph, thread_id, source="auto_after_approve")
                ):
                    yield event
        except Exception as exc:  # noqa: BLE001 - clean SSE error frame, same as ask_stream's backstop
            logger.exception("sprint recovery decision stream failed")
            yield _sse("error", {"detail": "sprint recovery request failed"})
            return
        final = await graph.aget_state(config)
        _register_if_waiting(request, sprint_id, thread_id, final.values)
        await _notify_if_escalated(request, thread_id, sprint_id, final.values)
        _track_index_lag_if_timed_out(final.values)
        yield _sse("result", (await _status_response_with_summary(thread_id, final.values, graph)).model_dump(by_alias=True))

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/{thread_id}/retry", response_model=RecoveryStatusResponse)
async def retry_recovery_endpoint(thread_id: str, request: Request) -> RecoveryStatusResponse:
    graph = _graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    if snap.values.get("status") not in ("committing", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"sprint-recovery {thread_id} is not committing or failed (status={snap.values.get('status')})",
        )
    await retry_recovery_commit(graph, thread_id)
    final = await graph.aget_state(config)
    _register_if_waiting(request, snap.values["sprint_id"], thread_id, final.values)
    return _status_response(thread_id, final.values)


@router.post("/{thread_id}/trigger-reevaluation", response_model=RecoveryStatusResponse)
async def trigger_reevaluation_endpoint(thread_id: str, request: Request) -> RecoveryStatusResponse:
    """The manual half of the "human or Kafka event, same protocol" pause — see
    `wait_for_reevaluation_node`'s docstring. A real `IssueContentChangedEvent` calls the identical
    `trigger_reevaluation` function from `kafka_trigger.py` instead of from this endpoint.
    """
    graph = _graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    if snap.values.get("status") != "waiting_reevaluation":
        # Not an error — a race this workflow is *designed* to have. `kafka_trigger.py` resumes the
        # exact same pause whenever a relevant `IssueContentChangedEvent` lands, and (found live) the
        # workflow's own committed actions produce those events: approving a plan writes
        # change_priority/add_comment to jira-backend, jira-backend publishes, the trigger consumes,
        # and the pause is already consumed by the time a human clicks "Re-check now" a second later.
        # Both callers ask the same question — "what's the latest?" — so answering with the current
        # state is correct; 409-ing surfaced a raw error blob for what is really a completed action.
        return await _status_response_with_summary(thread_id, snap.values, graph)
    await trigger_reevaluation(graph, thread_id, source="manual")
    final = await graph.aget_state(config)
    _register_if_waiting(request, snap.values["sprint_id"], thread_id, final.values)
    await _notify_if_escalated(request, thread_id, snap.values["sprint_id"], final.values)
    _track_index_lag_if_timed_out(final.values)
    return await _status_response_with_summary(thread_id, final.values, graph)


@router.post("/{thread_id}/trigger-reevaluation/stream")
async def trigger_reevaluation_stream_endpoint(thread_id: str, request: Request) -> StreamingResponse:
    """SSE variant of POST /{thread_id}/trigger-reevaluation. `reevaluate_node` itself is fast and
    deterministic (no LLM call — see `_detect_risk_signals`), but if it decides the sprint is still at
    risk and under the escalation cap, it routes straight into `plan_node` again in the same call —
    exactly the case worth a `stage` event for. See `_stream_call`.
    """
    graph = _graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    sprint_id = snap.values["sprint_id"]

    if snap.values.get("status") != "waiting_reevaluation":
        # Same benign race as the non-streaming endpoint above — see its comment. Answered as a
        # normal `result` frame so the client's stream handler needs no special case for it.
        already = await _status_response_with_summary(thread_id, snap.values, graph)

        async def already_done() -> AsyncIterator[str]:
            yield _sse("result", already.model_dump(by_alias=True))

        return StreamingResponse(already_done(), media_type="text/event-stream")

    async def generate() -> AsyncIterator[str]:
        try:
            async for event in _stream_call(trigger_reevaluation(graph, thread_id, source="manual")):
                yield event
        except Exception as exc:  # noqa: BLE001 - clean SSE error frame, same as ask_stream's backstop
            yield _sse("error", {"detail": str(exc)})
            return
        final = await graph.aget_state(config)
        _register_if_waiting(request, sprint_id, thread_id, final.values)
        await _notify_if_escalated(request, thread_id, sprint_id, final.values)
        _track_index_lag_if_timed_out(final.values)
        yield _sse("result", (await _status_response_with_summary(thread_id, final.values, graph)).model_dump(by_alias=True))

    return StreamingResponse(generate(), media_type="text/event-stream")


class CheckpointOut(_CamelModel):
    checkpoint_id: str
    next_node: Optional[str]
    status: Optional[str]
    # When this checkpoint was written. LangGraph records it on every snapshot; without surfacing it
    # the time-travel picker was an unordered-looking wall of near-identical rows with no way to tell
    # which step happened first — the single most confusing thing about that panel.
    created_at: Optional[str] = None
    # What made THIS checkpoint different from the last one, in the one case where the generic
    # per-node label can't say — `commit_one_action` runs once per action in a plan, so a 3-action
    # plan produced 3 identical-looking "Applying changes in Jira…" rows in a row with nothing to
    # tell them apart. Populated only for those rows; `None` everywhere else means "the label already
    # says enough."
    detail: Optional[str] = None


@router.get("/{thread_id}/history", response_model=List[CheckpointOut])
async def recovery_history_endpoint(thread_id: str, request: Request) -> List[CheckpointOut]:
    graph = _graph(request)
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    history = await list_checkpoint_history(graph, thread_id)
    history = [s for s in history if s.values]  # drop the empty pre-START snapshot
    details = _committing_step_details(history)
    return [
        CheckpointOut(
            checkpoint_id=s.config["configurable"]["checkpoint_id"],
            next_node=s.next[0] if s.next else None,
            status=s.values.get("status"),
            created_at=getattr(s, "created_at", None),
            detail=details.get(s.config["configurable"]["checkpoint_id"]),
        )
        for s in history
    ]


def _committing_step_details(history_newest_first: list) -> Dict[str, str]:
    """Maps each `status="committing"` checkpoint to which specific action it just applied, by diffing
    `committed_actions` against the immediately-preceding checkpoint in time (history comes in
    newest-first, so "preceding" is the *next* item in this list). One index appears per step because
    `commit_one_action_node` commits exactly one action per graph step (see that node's docstring on
    why — a crash mid-plan must not lose track of which actions already ran); this walk surfaces that
    one-per-step reality back to the reader instead of leaving every step looking identical.
    """
    result: Dict[str, str] = {}
    for i, snap in enumerate(history_newest_first):
        if snap.values.get("status") != "committing":
            continue
        plan = snap.values.get("approved_plan")
        if plan is None:
            continue
        prev = history_newest_first[i + 1] if i + 1 < len(history_newest_first) else None
        before = set((prev.values.get("committed_actions") or {}).keys()) if prev else set()
        after = set((snap.values.get("committed_actions") or {}).keys())
        newly_committed = after - before
        if len(newly_committed) != 1:
            continue  # ambiguous (0 or >1) — leave detail unset rather than guess wrong
        idx = int(next(iter(newly_committed)))
        if 0 <= idx < len(plan.actions):
            result[snap.config["configurable"]["checkpoint_id"]] = _describe_action(plan.actions[idx])
    return result


class TimeTravelRequest(_CamelModel):
    checkpoint_id: str = Field(min_length=1)
    note: str = Field(min_length=1)


@router.post("/{thread_id}/time-travel", response_model=RecoveryStatusResponse)
async def time_travel_endpoint(thread_id: str, req: TimeTravelRequest, request: Request) -> RecoveryStatusResponse:
    graph = _graph(request)
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    result = await time_travel_resume(graph, thread_id, req.checkpoint_id, req.note)
    return await _status_response_with_summary(thread_id, result, graph)
