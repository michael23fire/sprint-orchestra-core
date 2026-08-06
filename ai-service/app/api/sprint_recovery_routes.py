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
from app.sprint_recovery.graph import (
    ON_STAGE_VAR,
    build_escalation_summary,
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


def _register_if_waiting(request: Request, sprint_id: int, thread_id: str, values: dict) -> None:
    """Tells the Kafka trigger consumer (if running) to watch for events relevant to this sprint the
    moment a workflow actually reaches `wait_for_reevaluation` — see kafka_trigger.py's own docstring
    for why this in-process registration doesn't survive an ai-service restart, a known, named gap.
    """
    if values.get("status") != "waiting_reevaluation":
        return
    trigger = getattr(request.app.state, "sprint_recovery_kafka_trigger", None)
    if trigger is not None:
        trigger.register_waiting(sprint_id, thread_id)


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
        values["sprint_name"], summary,
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
    token_usage: int
    error: Optional[str]
    # Only ever populated when status=="escalated" — see build_escalation_summary's docstring. None the
    # rest of the time rather than an empty string, so the frontend can tell "not applicable" apart
    # from "computed and genuinely empty" (the latter can't actually happen, but the type says so).
    escalation_summary: Optional[str] = None


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
        token_usage=values.get("token_usage", 0),
        error=values.get("error"),
    )


async def _status_response_with_summary(thread_id: str, values: dict, graph) -> RecoveryStatusResponse:
    """`_status_response` plus `escalation_summary` when status=="escalated" — separate from that sync
    function because computing the summary needs an awaited `aget_state_history` walk. Safe to call on
    every status, not just the moment of transition: unlike `_notify_if_escalated` (fires once, only on
    a real transition), this is read-only and just makes "escalated" viewable correctly whenever this
    thread's status is fetched — including reopening the modal long after the transition happened.
    """
    response = _status_response(thread_id, values)
    if response.status == "escalated":
        response.escalation_summary = await build_escalation_summary(graph, thread_id)
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
    _register_if_waiting(request, snap.values["sprint_id"], thread_id, final.values)
    return _status_response(thread_id, final.values)


@router.post("/{thread_id}/decision/stream")
async def submit_recovery_decision_stream_endpoint(
    thread_id: str, req: DecisionRequest, request: Request
) -> StreamingResponse:
    """SSE variant of POST /{thread_id}/decision — only `decision="revise"` ever makes a real LLM call
    (it re-enters `plan_node`), but every decision type goes through this so the frontend has one call
    to make regardless of which button was clicked; approve/edit/reject just won't emit a `stage` event
    before their `result` arrives. See `_stream_call`.
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
        except Exception as exc:  # noqa: BLE001 - clean SSE error frame, same as ask_stream's backstop
            logger.exception("sprint recovery decision stream failed")
            yield _sse("error", {"detail": "sprint recovery request failed"})
            return
        final = await graph.aget_state(config)
        _register_if_waiting(request, sprint_id, thread_id, final.values)
        yield _sse("result", _status_response(thread_id, final.values).model_dump(by_alias=True))

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
        raise HTTPException(
            status_code=409,
            detail=f"sprint-recovery {thread_id} is not waiting on reevaluation (status={snap.values.get('status')})",
        )
    await trigger_reevaluation(graph, thread_id, source="manual")
    final = await graph.aget_state(config)
    _register_if_waiting(request, snap.values["sprint_id"], thread_id, final.values)
    await _notify_if_escalated(request, thread_id, snap.values["sprint_id"], final.values)
    return await _status_response_with_summary(thread_id, final.values, graph)


class CheckpointOut(_CamelModel):
    checkpoint_id: str
    next_node: Optional[str]
    status: Optional[str]


@router.get("/{thread_id}/history", response_model=List[CheckpointOut])
async def recovery_history_endpoint(thread_id: str, request: Request) -> List[CheckpointOut]:
    graph = _graph(request)
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no sprint-recovery workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    history = await list_checkpoint_history(graph, thread_id)
    return [
        CheckpointOut(
            checkpoint_id=s.config["configurable"]["checkpoint_id"],
            next_node=s.next[0] if s.next else None,
            status=s.values.get("status"),
        )
        for s in history
        if s.values  # skip the empty pre-START snapshot
    ]


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
