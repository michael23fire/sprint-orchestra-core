"""State shape for the sprint-recovery workflow (app/sprint_recovery/graph.py). Plain TypedDict — same
reasoning as `app/planning/rollout_schemas.py::RolloutState`: nothing outside this module's own nodes
writes it directly, and it has to be a plain-serializable shape for `AsyncPostgresSaver` to checkpoint.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional, TypedDict

from app.sprint_recovery.schemas import (
    EvidenceItem,
    RecoveryPlan,
    RiskSignal,
    RootCauseHypothesis,
    SprintSnapshot,
)

RecoveryStatus = Literal[
    "diagnosing",          # gathering evidence / generating hypotheses
    "no_risk_found",       # zero deterministic risk signals — healthy sprint, terminal, no LLM call made
    "awaiting_clarification",
    "awaiting_plan_approval",
    "revising",            # plan rejected with feedback, looping back into plan_node
    "committing",
    "committed",
    "waiting_reevaluation",  # actions committed, waiting on a Kafka event or manual trigger
    "recovered",
    "escalated",
    "rejected",
    "failed",
]


class SprintRecoveryState(TypedDict):
    # --- Set once ---
    space_id: int
    sprint_id: int
    sprint_name: str
    user_id: str
    username: str
    max_clarification_rounds: int
    max_escalation_rounds: int
    max_token_budget: int

    # --- Diagnosis ---
    risk_signals: List[RiskSignal]
    evidence: List[EvidenceItem]
    hypotheses: List[RootCauseHypothesis]
    clarification_rounds: int
    clarification_question: Optional[str]
    clarification_answer: Optional[str]

    # --- Sprint-wide context (see SprintSnapshot: previously computed then discarded) ---
    sprint_snapshot: Optional[SprintSnapshot]

    # --- Plans / approval ---
    plans: List[RecoveryPlan]
    decision: Optional[Literal["approve", "edit", "reject", "revise"]]
    approved_plan: Optional[RecoveryPlan]
    # A human rejecting every plan with feedback ("none of these are right, try X instead") loops back
    # into `plan_node` instead of ending the workflow — same shape as the escalation loop's prior_note,
    # just triggered by a human's own dissatisfaction instead of a reevaluate finding the sprint still
    # at risk. Bounded by max_plan_revision_rounds for the same reason every other loop here is bounded.
    human_plan_feedback: Optional[str]
    plan_revision_round: int
    max_plan_revision_rounds: int

    # --- Execution (idempotency ledger) ---
    committed_actions: Dict[str, str]  # action_index (str) -> result summary
    # issue_key -> that issue's `updatedAt` read back from jira-backend (the source of truth) right
    # after a write to it committed. `reevaluate_node` waits for the read model to catch up to these
    # before recomputing risk — the deterministic replacement for a fixed "hope 2 seconds is enough"
    # sleep. See `_await_index_catch_up` in graph.py.
    index_watermarks: Dict[str, str]
    # issue_key -> that issue's `updatedAt` as it was *before* this workflow wrote to it. **Found
    # live**: jira-backend's `@PreUpdate` stamps `updatedAt = now()` on any issue write, and
    # `long_in_progress`/`blocked_no_flag` use `updated_at` to measure "how long has nobody touched
    # this" — so committing a `change_priority` reset the very clock the risk detection reads, and the
    # next reevaluation reported `recovered` on a sprint where nothing had actually been done. This is
    # the baseline that lets `_detect_risk_signals` discount the workflow's own writes: an automated
    # priority bump is not somebody making progress. See `_detect_risk_signals`'s `own_writes` param.
    pre_write_updated_at: Dict[str, str]
    # Set fresh on every reevaluate_node run (never sticky from a prior round) — True only when
    # `_await_index_catch_up` hit its timeout without confirming the read model caught up. Staleness can
    # only ever make the sprint look *more* at risk than it really is (a stale read can't see a fix that
    # already landed, but it also can't invent a new problem that hasn't happened yet), so this is
    # surfaced as a caveat on an at-risk-looking result, not treated as a reason to distrust a
    # "recovered" one.
    index_catch_up_timed_out: bool
    token_usage: int

    # --- Escalation loop ---
    escalation_round: int
    completion_forecast_pct: Optional[float]
    # Plain-language description of every action actually committed in a PRIOR round — never wiped the
    # way `committed_actions` is when a new round starts (see reevaluate_node), so `plan_node` can tell
    # a genuinely new action apart from a no-op repeat of something already done (e.g. re-raising a
    # priority that's already at the ceiling). Found live: without this, round 2 raised the same 3
    # issues' priority to "highest" a second time, because the prompt only said "the previous plan
    # didn't work, don't repeat it" without ever saying what the previous plan actually did.
    prior_committed_actions: List[str]

    status: RecoveryStatus
    error: Optional[str]


def initial_recovery_state(
    space_id: int, sprint_id: int, sprint_name: str, user_id: str, username: str,
    max_clarification_rounds: int = 2, max_escalation_rounds: int = 2, max_token_budget: int = 200_000,
    max_plan_revision_rounds: int = 2,
) -> SprintRecoveryState:
    return SprintRecoveryState(
        space_id=space_id, sprint_id=sprint_id, sprint_name=sprint_name,
        user_id=user_id, username=username,
        max_clarification_rounds=max_clarification_rounds,
        max_escalation_rounds=max_escalation_rounds,
        max_token_budget=max_token_budget,
        risk_signals=[], evidence=[], hypotheses=[],
        clarification_rounds=0, clarification_question=None, clarification_answer=None,
        sprint_snapshot=None,
        plans=[], decision=None, approved_plan=None,
        human_plan_feedback=None, plan_revision_round=0, max_plan_revision_rounds=max_plan_revision_rounds,
        committed_actions={}, index_watermarks={}, pre_write_updated_at={},
        index_catch_up_timed_out=False, token_usage=0,
        escalation_round=0, completion_forecast_pct=None, prior_committed_actions=[],
        status="diagnosing", error=None,
    )
