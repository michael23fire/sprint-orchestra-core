"""Sprint recovery: diagnose -> gather evidence -> hypothesize (grounded) -> confidence-gated
clarification -> propose recovery plans -> durable human approval -> idempotent execution ->
wait-and-reevaluate -> escalate or close.

**Honest framing, stated up front, the same way `app/planning/graph.py` and
`app/planning/rollout_graph.py` both do.** The control flow here — a handful of nodes, a couple of
bounded conditional loops — does not *require* LangGraph; a `status` column plus explicit resume
functions would work. What's different from the rollout workflow (which also doesn't require it) is
where the honest case for using it anyway gets *stronger*, not where it becomes strictly necessary:

1. **Multiple, differently-shaped pauses**, not one. The rollout workflow has exactly one interrupt
   shape (approve/edit/reject a finished plan). This graph has at least three: `clarify` (answer one
   specific question mid-diagnosis), `approve_plan` (approve/edit/reject a finished plan, same shape as
   before), and — after execution — a wait that is resumed by *either* a real Kafka event or a human,
   not only a human. A hand-rolled version would need a distinct status value and a bespoke resume
   handler per shape; LangGraph's `interrupt()` gives each pause the same protocol regardless of shape.
2. **Re-entry driven by an external event, not only a human click.** `waiting_reevaluation` is resumed
   by a real Kafka consumer (`app/sprint_recovery/kafka_trigger.py`) reacting to
   `IssueContentChangedEvent` on the *same* topic `vectorization-service` already consumes — this
   graph does not invent new event infrastructure, it adds a second consumer group on an existing one.
3. **A genuinely conditional replan loop**, bounded by `max_escalation_rounds` — not just "retry the
   same thing," but "generate new plans given what happened last time," which is the shape
   `crag_loop.py`'s docstring explicitly says a framework earns its keep on once there are *several*
   distinct decision points, not one.

None of this is "impossible without a framework" — it's "the hand-rolled version's status-string
branching gets meaningfully more error-prone here than it was for the rollout workflow," which is a
cost judgment, not a capability claim.

**Grounding**: mirrors `crag_loop.py`'s `_ground_answer` — every `RootCauseHypothesis.
supporting_evidence_ids` must reference an `EvidenceItem.citation_id` actually gathered this run.
Unlike `_ground_answer`'s hard reject-the-whole-answer behavior, `_apply_grounding` here drops only the
ungrounded hypothesis (partial evidence for other hypotheses is still useful), logging what was cut.

**Runaway-loop protection**: `max_clarification_rounds` bounds the diagnose<->clarify cycle,
`max_escalation_rounds` bounds the commit<->replan cycle, `max_token_budget` is a hard cost ceiling
checked before every LLM call — matches the existing `MAX_REVISION_ROUNDS`/`max_tool_iterations`
pattern already established in `app/planning/graph.py` / `app/agent/crag_loop.py`, applied here to two
independent loops instead of one.
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Callable, List, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace

# One span per graph *node* execution, nested under whichever parent span the calling route already
# opened (app/api/sprint_recovery_routes.py mirrors app/api/routes.py's `/ask` pattern: one parent
# span per HTTP call, everything inside it nests automatically via OpenTelemetry context propagation).
# Without this, Phoenix would show a flat list of disconnected LLM-call spans with no way to tell
# "these three calls were the diagnose step, this one was plan" — the exact debugging question a
# multi-step agent gets asked about. `thread_id` is tagged on every span specifically because this
# workflow spans multiple, separate HTTP calls (sometimes days apart) — no single Phoenix trace covers
# the whole thread's lifetime, so the thread_id attribute is what makes its spans findable/correlatable
# across traces, not the trace tree itself.
_tracer = trace.get_tracer(__name__)

# Optional SSE progress-label plumbing — same `contextvars.ContextVar` mechanism `crag_loop.py` uses
# for its per-request `stage_seconds` (needed for the identical reason: `build_sprint_recovery_graph`
# builds one shared, long-lived compiled graph at startup, so per-*request* state like "which HTTP call
# wants stage events" can't be a constructor argument — it has to travel via context, scoped to
# whichever request's call stack is currently executing a node). Mirrors `POST /ask/stream`'s `on_stage`
# pattern (app/api/routes.py) exactly: `asyncio.create_task` snapshots the current contextvars.Context
# at creation time, so the streaming endpoint sets this *before* creating the `graph.ainvoke` task, and
# every node's span wrapper below picks it up automatically without any change to `diagnose_node`/
# `plan_node`'s own signatures. `None` (the default) is a real no-op, not just "nothing subscribed yet"
# — every non-streaming caller (tests, the plain JSON endpoints) leaves this unset.
ON_STAGE_VAR: ContextVar[Optional[Callable[[str], None]]] = ContextVar("sprint_recovery_on_stage", default=None)

# Human-readable labels for the two nodes that actually make an LLM call — the only ones worth
# surfacing as a stage event. `approval`/`clarify`/`wait_for_reevaluation` are pauses (nothing to wait
# on), and `commit_one_action` is a fast Jira REST call, not an LLM call.
_STAGE_LABELS = {
    "diagnose": "detecting risk signals and analyzing evidence",
    "plan": "generating recovery plans",
}

# Same pattern `crag_loop.py`'s `_ground_answer` already uses to recognize an issue key mentioned in
# free text (e.g. a comment saying "waiting on PAY-97" without PAY-97 itself being independently
# flagged as at-risk) — reused rather than reinvented, and for the same reason: a hypothesis about an
# *undocumented* dependency inherently needs to reference an issue that was never flagged on its own.
_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b", re.IGNORECASE)

from app.auth.space_membership import SpaceMembershipChecker, SpaceMembershipError
from app.sprint_recovery.jira_actions_client import JiraActionError, JiraActionsClient
from app.sprint_recovery.schemas import (
    DiagnosisResult,
    EvidenceItem,
    RecoveryAction,
    RecoveryPlan,
    RecoveryPlanSet,
    RiskSignal,
    RootCauseHypothesis,
)
from app.sprint_recovery.state import SprintRecoveryState

logger = logging.getLogger(__name__)

# A single revision pass on the clarify loop and a single replan pass on escalation are the
# structural minimum to demonstrate the loop actually works — the *count* itself is a caller-supplied
# ceiling (SprintRecoveryState.max_clarification_rounds / max_escalation_rounds), not hardcoded here.
_TOKENS_PER_LLM_CALL_ESTIMATE = 4000  # coarse, pre-call budget check — see _check_budget's docstring


def _check_budget(state: SprintRecoveryState) -> bool:
    """True if there's room for one more LLM call. Coarse and pre-emptive on purpose: the real
    per-call token usage is only known *after* the call, but the whole point of a budget ceiling is to
    stop *before* spending more, not to notice afterward that too much was already spent.
    """
    return state["token_usage"] + _TOKENS_PER_LLM_CALL_ESTIMATE <= state["max_token_budget"]


async def _gather_evidence(
    retrieval, space_id: int, sprint_id: int, flagged_issue_keys: List[str],
) -> List[EvidenceItem]:
    """Deterministic, unranked fetch — same "get_issue_comments/details/attachments as a fallback to
    top-K search" reasoning `crag_loop.py`'s tool surface already uses, applied here as the *only*
    retrieval path (there's no model in the loop yet at this point to decide whether to search more).
    """
    evidence: List[EvidenceItem] = []
    n = 0
    if not flagged_issue_keys:
        return evidence
    comments = await retrieval.get_issue_comments([space_id], flagged_issue_keys)
    for c in comments.comments:
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=c["issue_key"], source_type="comment", content=c["content"],
        ))
    details = await retrieval.get_issue_details([space_id], flagged_issue_keys)
    for d in details.details:
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=d["issue_key"], source_type="description", content=d["content"],
        ))
    attachments = await retrieval.get_issue_attachments([space_id], flagged_issue_keys)
    for a in attachments.attachments:
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=a["issue_key"], source_type="attachment", content=a["content"],
        ))
    history = await retrieval.query_issue_history([space_id], {"issue_keys": flagged_issue_keys, "limit": 50})
    for h in history.changes:
        # **Found live, not hypothetically**: vectorization-service's /issues/history returns more than
        # field-change rows — `issue_created`/`comment_created`/`code_link_added`/etc all come back with
        # `field_name`/`from_value`/`to_value` all null and the actual human-readable content in
        # `description` instead (e.g. "linked pull request 119"). The original unconditional
        # `f"{field_name}: {from_value} -> {to_value}"` template rendered those as literal, useless
        # `"None: None -> None"` evidence — real signal silently destroyed by a formatting bug, not
        # dropped on purpose. Also drops `issueOrder` changes outright: drag-and-drop backlog
        # reordering, never diagnostically relevant, pure noise that was diluting the evidence count.
        field_name = h.get("field_name")
        if field_name == "issueOrder":
            continue
        if field_name:
            content = f"{field_name}: {h['from_value']} -> {h['to_value']} ({h['changed_at']})"
        elif h.get("description"):
            content = f"{h.get('event_type', 'change')}: {h['description']} ({h['changed_at']})"
        else:
            continue  # neither a field change nor a description — nothing to say, don't manufacture evidence
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=h["issue_key"], source_type="history", content=content,
        ))
    return evidence


async def _detect_risk_signals(retrieval, space_id: int, sprint_id: int) -> List[RiskSignal]:
    """Deterministic — code computes this, never the LLM (same "code computes, LLM explains" split
    `app/sprint_health/service.py` and `app/planning/graph.py`'s critic already use). Two signals,
    chosen because both are directly computable from `query_issues`'s existing fields without
    inventing data this project doesn't have (e.g. no assignee-workload signal — `IssueRowOut` carries
    no assignee field, so that signal type stays in the schema as an LLM-inferable-from-evidence
    category rather than a fabricated deterministic one).
    """
    result = await retrieval.query_issues([space_id], {"sprint_ids": [sprint_id], "limit": 200})
    signals: List[RiskSignal] = []
    for issue in result.issues:
        status = (issue.get("status") or "").lower()
        if status in ("in_progress", "in progress") and issue.get("updated_at"):
            signals.append(RiskSignal(
                issue_key=issue["issue_key"], signal_type="long_in_progress",
                description=f"{issue['issue_key']} has been in_progress, last updated {issue['updated_at']}.",
            ))
    total = result.total_count
    done = result.counts_by_status.get("done", 0) + result.counts_by_status.get("completed", 0)
    if total > 0:
        forecast = round(100 * done / total, 1)
        if forecast < 70:
            signals.append(RiskSignal(
                signal_type="low_completion_forecast",
                description=f"{done}/{total} issues done ({forecast}%) — below the 70% checkpoint.",
            ))
    return signals


def _apply_grounding(hypotheses: List[RootCauseHypothesis], evidence: List[EvidenceItem]) -> List[RootCauseHypothesis]:
    """Drops any hypothesis citing an evidence id that was never actually gathered — mirrors
    `crag_loop.py`'s `_ground_answer` hard-reject rule, scoped per-hypothesis instead of per-answer so
    one ungrounded claim doesn't discard otherwise-valid ones.
    """
    valid_ids = {e.citation_id for e in evidence}
    kept = []
    for h in hypotheses:
        if not h.supporting_evidence_ids:
            logger.warning("dropping hypothesis with no supporting_evidence_ids", extra={"statement": h.statement})
            continue
        if not set(h.supporting_evidence_ids).issubset(valid_ids):
            logger.warning(
                "dropping ungrounded hypothesis citing unknown evidence ids",
                extra={"statement": h.statement, "cited": h.supporting_evidence_ids},
            )
            continue
        kept.append(h)
    return kept


async def diagnose_node(state: SprintRecoveryState, client, model: str, retrieval) -> dict:
    if not _check_budget(state):
        return {"status": "failed", "error": "token budget exhausted before diagnosis could complete"}

    signals = await _detect_risk_signals(retrieval, state["space_id"], state["sprint_id"])
    flagged_keys = sorted({s.issue_key for s in signals if s.issue_key})
    evidence = await _gather_evidence(retrieval, state["space_id"], state["sprint_id"], flagged_keys)
    if state["clarification_answer"]:
        evidence = evidence + [EvidenceItem(
            citation_id=f"human-clarification-{state['clarification_rounds']}",
            issue_key="", source_type="comment", content=state["clarification_answer"],
        )]

    evidence_text = "\n".join(f"[{e.citation_id}] ({e.issue_key} / {e.source_type}) {e.content}" for e in evidence)
    signals_text = "\n".join(f"- {s.signal_type}: {s.description}" for s in signals)
    prompt = (
        f"Sprint: {state['sprint_name']}\n\nDeterministic risk signals:\n{signals_text}\n\n"
        f"Evidence gathered (cite by id, never invent an id):\n{evidence_text}\n\n"
        "Propose root-cause hypotheses for why this sprint may be at risk. Every hypothesis must cite "
        "at least one real evidence id above. Set overall_confidence='insufficient' only if there is a "
        "genuine, specific gap a human could fill — not as a default hedge."
    )
    result: DiagnosisResult = await client.chat.completions.create(
        model=model, response_model=DiagnosisResult, max_retries=2,
        messages=[{"role": "user", "content": prompt}],
    )
    grounded = _apply_grounding(result.hypotheses, evidence)
    update = {
        "risk_signals": signals, "evidence": evidence, "hypotheses": grounded,
        "token_usage": state["token_usage"] + _TOKENS_PER_LLM_CALL_ESTIMATE,
    }
    if result.overall_confidence == "insufficient" and state["clarification_rounds"] < state["max_clarification_rounds"]:
        update["status"] = "diagnosing"  # will route to clarify
        update["clarification_question"] = result.clarifying_question
    else:
        update["status"] = "diagnosing"  # will route to plan
        update["clarification_question"] = None
    return update


def _after_diagnose(state: SprintRecoveryState) -> str:
    if state["status"] == "failed":
        return END
    if state["clarification_question"] and state["clarification_rounds"] < state["max_clarification_rounds"]:
        return "clarify"
    return "plan"


async def clarify_node(state: SprintRecoveryState) -> dict:
    """Pauses for exactly one specific question, then loops back to `diagnose` with the answer folded
    in as evidence — this is the confidence-gated pause, distinct in shape from the plan-approval pause
    below (it carries a free-text answer, not a decision enum).
    """
    answer = interrupt({"question": state["clarification_question"], "round": state["clarification_rounds"] + 1})
    return {
        "clarification_answer": answer.get("answer", ""),
        "clarification_rounds": state["clarification_rounds"] + 1,
        "status": "diagnosing",
    }


async def plan_node(state: SprintRecoveryState, client, model: str) -> dict:
    """**Found live, not hypothetically**: the first end-to-end run against real AtlasCart data
    produced actions with `target_issue_key` values like `"ev15"`, `"ev3"` — the model had confused
    `EvidenceItem.citation_id` (only ever meaningful for `RootCauseHypothesis.supporting_evidence_ids`)
    with a real issue key, because the original prompt showed it citation ids without ever separately
    listing which issue keys actually exist. Fixed two ways, not just one: the prompt now lists real
    issue keys as their own explicit, separately-labeled set (never call it "evidence" or "citations"
    in that list), *and* `_validate_plan_issue_keys` below drops — doesn't silently keep — any action
    naming a key outside that set, the same "prompt clarity is necessary but insufficient, verify in
    code" split every grounding check in this codebase already uses.
    """
    if not _check_budget(state):
        return {"status": "failed", "error": "token budget exhausted before plan generation could complete"}
    known_issue_keys = sorted(
        {e.issue_key for e in state["evidence"] if e.issue_key}
        | {s.issue_key for s in state["risk_signals"] if s.issue_key}
        | {k.upper() for e in state["evidence"] for k in _ISSUE_KEY_RE.findall(e.content)}
    )
    hyps_text = "\n".join(f"- ({h.confidence}) {h.statement} [cites: {', '.join(h.supporting_evidence_ids)}]" for h in state["hypotheses"])
    prior_note = ""
    if state["escalation_round"] > 0:
        prior_note = (
            f"\n\nThis is escalation round {state['escalation_round']}: the previously approved plan "
            "did not resolve the risk. Propose different or adjusted plans, not a repeat."
        )
    if state.get("human_plan_feedback"):
        prior_note += (
            f"\n\nA human reviewed the previous plans and rejected all of them with this feedback: "
            f"\"{state['human_plan_feedback']}\". Address this feedback directly in the new plans — do "
            "not repeat what was rejected."
        )
    prompt = (
        f"Sprint: {state['sprint_name']}\nRoot-cause hypotheses (the bracketed [cites: ...] ids are "
        f"internal evidence references, NEVER usable as target_issue_key):\n{hyps_text}{prior_note}\n\n"
        f"Real issue keys this plan may act on — target_issue_key/depends_on_issue_key MUST be one of "
        f"exactly these, verbatim, never an evidence citation id and never invented:\n"
        f"{', '.join(known_issue_keys)}\n\n"
        "Propose 1-3 concrete recovery plans — as many genuinely distinct, worthwhile approaches as "
        "actually exist for this situation. Do not pad the count to reach 2 or 3 with a weak or "
        "redundant option; if there is really only one sensible approach, propose just that one. "
        "Every action must use one of these action_types: "
        "link_dependency, change_priority, move_out_of_sprint, add_comment. If a hypothesis itself "
        "concludes the signal is a false positive, already resolved, or that evidence explicitly asks "
        "not to escalate/pull the issue, every proposed plan must respect that: prefer a conservative "
        "action (e.g. add_comment to confirm/close the loop) and do not offer move_out_of_sprint or a "
        "raised priority as an option for that issue, even as an alternative choice."
    )
    result: RecoveryPlanSet = await client.chat.completions.create(
        model=model, response_model=RecoveryPlanSet, max_retries=2,
        messages=[{"role": "user", "content": prompt}],
    )
    plans = _validate_plan_issue_keys(result.plans, known_issue_keys)
    if not plans:
        return {
            "status": "failed", "error": "every proposed plan cited only invalid/unknown issue keys",
            "token_usage": state["token_usage"] + _TOKENS_PER_LLM_CALL_ESTIMATE,
        }
    return {
        "plans": plans, "status": "awaiting_plan_approval",
        "token_usage": state["token_usage"] + _TOKENS_PER_LLM_CALL_ESTIMATE,
    }


def _validate_plan_issue_keys(plans: List[RecoveryPlan], known_issue_keys: List[str]) -> List[RecoveryPlan]:
    """Drops any action naming an issue key outside what was actually gathered as evidence/risk
    signals this run — the code-level backstop for the prompt fix above. A plan that loses all its
    actions this way is dropped entirely rather than shown to a human as an empty, unapprovable plan.
    """
    known = set(known_issue_keys)
    kept_plans = []
    for plan in plans:
        kept_actions = []
        for a in plan.actions:
            bad = {a.target_issue_key} | ({a.depends_on_issue_key} if a.depends_on_issue_key else set())
            bad -= known
            if bad:
                logger.warning("dropping action citing unknown issue key(s)", extra={"action_type": a.action_type, "bad_keys": list(bad)})
                continue
            kept_actions.append(a)
        if kept_actions:
            kept_plans.append(RecoveryPlan(
                plan_id=plan.plan_id, name=plan.name, rationale=plan.rationale,
                impact_on_goal=plan.impact_on_goal, actions=kept_actions,
            ))
        else:
            logger.warning("dropping plan with zero valid actions after issue-key validation", extra={"plan_id": plan.plan_id})
    return kept_plans


def _after_plan(state: SprintRecoveryState) -> str:
    return END if state["status"] == "failed" else "approval"


async def approval_node(state: SprintRecoveryState, space_membership: SpaceMembershipChecker) -> dict:
    """The plan-approval pause — same *shape* as `rollout_graph.py`'s `review_node` (approve/edit/
    reject a finished artifact), included here to show the two pause shapes side by side in one
    workflow rather than because this one alone would justify a new graph. Unlike rollout's binary
    approve-or-reject, this one has a third real path: `revise` — a human who doesn't like any of the
    offered plans but *does* have something concrete to say about why loops back into `plan_node` with
    that feedback folded in, rather than the workflow dead-ending on `reject`. Bounded by
    `max_plan_revision_rounds` for the same reason every other loop here is bounded; past the cap this
    degrades to a plain reject rather than looping forever.
    """
    payload = interrupt({"plans": [p.model_dump() for p in state["plans"]]})
    decision = payload.get("decision")

    try:
        await space_membership.validate(state["user_id"], state["username"], [state["space_id"]])
    except SpaceMembershipError as exc:
        return {"decision": "reject", "status": "rejected", "error": str(exc)}

    if decision == "revise":
        feedback = (payload.get("feedback") or "").strip()
        if not feedback:
            return {"decision": "reject", "status": "rejected", "error": "revise requires feedback text"}
        if state["plan_revision_round"] >= state["max_plan_revision_rounds"]:
            return {
                "decision": "reject", "status": "rejected",
                "error": f"reached the {state['max_plan_revision_rounds']}-revision limit without an "
                         "approved plan — rejecting rather than looping forever",
            }
        return {
            "decision": "revise", "status": "revising",
            "human_plan_feedback": feedback, "plan_revision_round": state["plan_revision_round"] + 1,
        }

    if decision == "reject":
        return {"decision": "reject", "status": "rejected"}

    plan_id = payload.get("planId") or payload.get("plan_id")
    chosen = next((p for p in state["plans"] if p.plan_id == plan_id), None)
    if chosen is None:
        return {"decision": "reject", "status": "failed", "error": f"unknown plan_id {plan_id!r}"}

    if decision == "edit" and payload.get("actions") is not None:
        chosen = RecoveryPlan(
            plan_id=chosen.plan_id, name=chosen.name, rationale=chosen.rationale,
            impact_on_goal=chosen.impact_on_goal,
            actions=[RecoveryAction.model_validate(a) for a in payload["actions"]],
        )

    return {"decision": decision or "approve", "approved_plan": chosen, "status": "committing", "committed_actions": {}}


def _after_approval(state: SprintRecoveryState) -> str:
    if state["status"] == "revising":
        return "plan"
    return END if state["status"] in ("rejected", "failed") else "commit_one_action"


async def commit_one_action_node(state: SprintRecoveryState, jira: JiraActionsClient) -> dict:
    """One action per graph step — identical reasoning to `rollout_graph.py::commit_one_node`: the
    checkpointer only persists between steps, so a crash mid-action-list must not be able to lose the
    record of which actions already executed.
    """
    plan = state["approved_plan"]
    remaining = [
        (i, a) for i, a in enumerate(plan.actions) if str(i) not in state["committed_actions"]
    ]
    if not remaining:
        return {"status": "waiting_reevaluation"}

    idx, action = remaining[0]
    try:
        result_summary = await jira.execute(state["space_id"], action, state["user_id"], state["username"])
    except JiraActionError as exc:
        return {"status": "failed", "error": str(exc)}

    committed = dict(state["committed_actions"])
    committed[str(idx)] = result_summary
    done = len(committed) == len(plan.actions)
    return {"committed_actions": committed, "status": "waiting_reevaluation" if done else "committing"}


def _after_commit_one_action(state: SprintRecoveryState) -> str:
    if state["status"] == "failed":
        return END
    if state["status"] == "waiting_reevaluation":
        return "wait_for_reevaluation"
    return "commit_one_action"


async def wait_for_reevaluation_node(state: SprintRecoveryState) -> dict:
    """The third, differently-shaped pause: unlike `clarify` (a free-text answer) and `approval` (a
    decision + optional edit), this one's resume payload can come from **two different kinds of
    caller** using the identical `Command(resume=...)` protocol — a human hitting "re-check now" in the
    UI, or `kafka_trigger.py`'s consumer reacting to a real `IssueContentChangedEvent` on an issue this
    workflow cares about. Both just call the same resume; this node doesn't need to know which.
    """
    interrupt({"waiting_since_committed_actions": len(state["committed_actions"]), "sprint_id": state["sprint_id"]})
    return {"status": "diagnosing"}


async def reevaluate_node(state: SprintRecoveryState, retrieval) -> dict:
    """Resumed either by a real Kafka event (`kafka_trigger.py`) or a manual trigger — re-runs the
    same deterministic risk detection used at the start, so "recovered" is a code-computed fact, not
    an LLM's opinion that things look better.
    """
    signals = await _detect_risk_signals(retrieval, state["space_id"], state["sprint_id"])
    forecast_signal = next((s for s in signals if s.signal_type == "low_completion_forecast"), None)
    still_at_risk = any(s.signal_type == "long_in_progress" for s in signals) or forecast_signal is not None
    if not still_at_risk:
        return {"status": "recovered", "risk_signals": signals}
    if state["escalation_round"] >= state["max_escalation_rounds"]:
        return {"status": "escalated", "risk_signals": signals}
    return {
        "status": "diagnosing", "risk_signals": signals, "escalation_round": state["escalation_round"] + 1,
        "committed_actions": {}, "approved_plan": None, "decision": None,
    }


def _after_reevaluate(state: SprintRecoveryState) -> str:
    if state["status"] == "diagnosing":
        return "plan"  # re-plan directly — hypotheses already exist, no need to re-diagnose from scratch
    return END


def _traced(node_name: str, fn):
    """Wraps a node function in one span per execution — see the module-level `_tracer` comment for
    why this is done here (wrapping the closures bound into the graph) rather than inside each node
    function body: it reaches every node uniformly, including `clarify_node`/`wait_for_reevaluation_node`
    (which take no extra dependencies to close over), without re-indenting each function's existing
    body. `thread_id` is read from LangGraph's own `RunnableConfig` (threaded through automatically by
    `ainvoke`), not from the state dict, since it's a run identity, not workflow data.
    """
    async def _wrapped(state: SprintRecoveryState, config=None) -> dict:
        thread_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
        on_stage = ON_STAGE_VAR.get()
        label = _STAGE_LABELS.get(node_name)
        if on_stage and label:
            on_stage(label)
        with _tracer.start_as_current_span(
            f"sprint_recovery.{node_name}",
            attributes={
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
                "sprint_recovery.thread_id": thread_id,
                "sprint_recovery.status_before": state.get("status", ""),
            },
        ) as span:
            result = await fn(state)
            if "status" in result:
                span.set_attribute("sprint_recovery.status_after", result["status"])
            return result
    return _wrapped


def build_sprint_recovery_graph(client, model: str, space_membership: SpaceMembershipChecker, retrieval, jira: JiraActionsClient):
    async def _diagnose(state: SprintRecoveryState) -> dict:
        return await diagnose_node(state, client, model, retrieval)

    async def _plan(state: SprintRecoveryState) -> dict:
        return await plan_node(state, client, model)

    async def _approval(state: SprintRecoveryState) -> dict:
        return await approval_node(state, space_membership)

    async def _commit_one(state: SprintRecoveryState) -> dict:
        return await commit_one_action_node(state, jira)

    async def _reevaluate(state: SprintRecoveryState) -> dict:
        return await reevaluate_node(state, retrieval)

    graph = StateGraph(SprintRecoveryState)
    graph.add_node("diagnose", _traced("diagnose", _diagnose))
    graph.add_node("clarify", _traced("clarify", clarify_node))
    graph.add_node("plan", _traced("plan", _plan))
    graph.add_node("approval", _traced("approval", _approval))
    graph.add_node("commit_one_action", _traced("commit_one_action", _commit_one))
    graph.add_node("wait_for_reevaluation", _traced("wait_for_reevaluation", wait_for_reevaluation_node))
    graph.add_node("reevaluate", _traced("reevaluate", _reevaluate))

    graph.add_edge(START, "diagnose")
    graph.add_conditional_edges("diagnose", _after_diagnose, {"clarify": "clarify", "plan": "plan", END: END})
    graph.add_edge("clarify", "diagnose")
    graph.add_conditional_edges("plan", _after_plan, {"approval": "approval", END: END})
    graph.add_conditional_edges(
        "approval", _after_approval, {"commit_one_action": "commit_one_action", "plan": "plan", END: END},
    )
    graph.add_conditional_edges(
        "commit_one_action", _after_commit_one_action,
        {"commit_one_action": "commit_one_action", "wait_for_reevaluation": "wait_for_reevaluation", END: END},
    )
    graph.add_edge("wait_for_reevaluation", "reevaluate")
    graph.add_conditional_edges("reevaluate", _after_reevaluate, {"plan": "plan", END: END})
    return graph


async def trigger_reevaluation(compiled_graph, thread_id: str, source: str, detail: str = "") -> dict:
    """Resumes the `wait_for_reevaluation` pause — the one interrupt in this graph answerable by
    either a human ("re-check now") or `kafka_trigger.py` reacting to a real Jira event, via the
    identical `Command(resume=...)` protocol. `source` is recorded for observability, not branched on.
    """
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread_id}}
    return await compiled_graph.ainvoke(Command(resume={"source": source, "detail": detail}), config=config)


async def retry_recovery_commit(compiled_graph, thread_id: str) -> dict:
    """Un-sticks the commit phase after `status in ("committing", "failed")` — same two-case split as
    `app/planning/rollout_graph.py::retry_rollout`, and for the identical reason: `status="committing"`
    left by a real crash already has a pending task (bare `ainvoke(None, ...)` is enough); a clean
    `status="failed"` reached a real `END` with nothing pending, so `approval`'s outgoing edge has to be
    replayed via `as_node` to re-arm `commit_one_action`. See that sibling function's docstring for why
    `as_node` names the node whose edge should fire, not the node that reruns — verified there, reused
    here rather than re-verified from scratch.
    """
    config = {"configurable": {"thread_id": thread_id}}
    snap = await compiled_graph.aget_state(config)
    if snap.values.get("status") == "committing":
        return await compiled_graph.ainvoke(None, config=config)
    await compiled_graph.aupdate_state(config, {"status": "committing", "error": None}, as_node="approval")
    return await compiled_graph.ainvoke(None, config=config)


async def list_checkpoint_history(compiled_graph, thread_id: str) -> list:
    """All checkpoints for this thread, newest first — what the UI's time-travel picker lists from.
    `next` (which node would run next from that point) is what makes a checkpoint meaningfully
    "rewindable to" vs. just a log entry — e.g. a checkpoint with `next=('plan',)` is the point right
    after diagnosis finished, before any plan existed yet.
    """
    config = {"configurable": {"thread_id": thread_id}}
    return [s async for s in compiled_graph.aget_state_history(config)]


async def build_escalation_summary(compiled_graph, thread_id: str) -> str:
    """Synthesizes what was actually tried across every escalation round into one human-readable
    paragraph — for the moment `reevaluate_node` gives up and hands off to a human, who shouldn't have
    to reconstruct "what did the automation already attempt" from a raw checkpoint list themselves.

    Reuses the same `aget_state_history` walk `list_checkpoint_history` already does for the
    time-travel picker rather than a separate accumulating state field: every checkpoint LangGraph
    stores is already a full state snapshot (already relied on for time-travel to work at all), and a
    `status="waiting_reevaluation"` checkpoint is exactly "one approved plan just finished committing"
    — one such checkpoint per escalation round, with `approved_plan`/`committed_actions` already sitting
    right there in that snapshot's `values`. Nothing new to track, just a different read of what's
    already durably recorded.

    Deliberately NOT called from inside `reevaluate_node`: a graph node only has the state dict, not the
    compiled graph object `aget_state_history` needs — and the current checkpoint isn't even saved yet
    while the node producing it is still running, so a node cannot read its own workflow's history
    mid-execution regardless. This is meant to be called from the caller that just resumed the graph and
    observed status=="escalated" in the result, same shape as `_register_if_waiting`'s "react to the
    status *after* the fact" pattern in sprint_recovery_routes.py.
    """
    config = {"configurable": {"thread_id": thread_id}}
    history = [s async for s in compiled_graph.aget_state_history(config)]
    history.reverse()  # aget_state_history yields newest-first; want chronological order here

    rounds: list = []
    seen_rounds = set()
    for snap in history:
        if snap.values.get("status") != "waiting_reevaluation":
            continue
        plan = snap.values.get("approved_plan")
        if plan is None:
            continue
        round_num = snap.values.get("escalation_round", 0)
        if round_num in seen_rounds:
            continue  # aget_state_history can repeat a snapshot around branch points (e.g. time-travel)
        seen_rounds.add(round_num)
        committed = snap.values.get("committed_actions") or {}
        actions = [committed[k] for k in sorted(committed, key=int)]
        rounds.append({"round": round_num, "plan_name": plan.name, "rationale": plan.rationale, "actions": actions})

    if not rounds:
        return "No recovery plan was ever committed before this workflow reached its escalation cap."

    lines = [
        f"Round {r['round'] + 1}: approved \"{r['plan_name']}\" ({r['rationale']}). "
        f"Actions taken: {'; '.join(r['actions']) if r['actions'] else 'none'}."
        for r in rounds
    ]
    final_signals = (history[-1].values.get("risk_signals") if history else None) or []
    if final_signals:
        lines.append("Still at risk because: " + " ".join(s.description for s in final_signals))
    return " ".join(lines)


async def time_travel_resume(compiled_graph, thread_id: str, target_checkpoint_id: str, note: str) -> dict:
    """Rewinds to an earlier checkpoint and continues forward *from there*, with `note` folded in as
    if a human had just answered a clarifying question at that point — same `as_node="clarify"` trick
    (clarify's own outgoing edge unconditionally goes to `diagnose`, so this re-enters diagnosis with
    the new context, regardless of which later node the original run had already reached). This
    rewrites the thread's own forward history rather than forking a new thread_id — verified directly:
    after resuming from a historical checkpoint, `aget_state(config)` on the *original* thread_id
    reflects the new branch, and the pre-rewind future is still visible via `aget_state_history`, not
    deleted.
    """
    target_config = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": target_checkpoint_id}
    }
    forked_config = await compiled_graph.aupdate_state(
        target_config, {"clarification_answer": note, "clarification_rounds": 0}, as_node="clarify",
    )
    return await compiled_graph.ainvoke(None, config=forked_config)
