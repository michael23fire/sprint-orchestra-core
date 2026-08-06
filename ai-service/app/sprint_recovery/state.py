"""State shape for the sprint-recovery workflow (app/sprint_recovery/graph.py). Plain TypedDict — same
reasoning as `app/planning/rollout_schemas.py::RolloutState`: nothing outside this module's own nodes
writes it directly, and it has to be a plain-serializable shape for `AsyncPostgresSaver` to checkpoint.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional, TypedDict

from app.sprint_recovery.schemas import EvidenceItem, RecoveryPlan, RiskSignal, RootCauseHypothesis

RecoveryStatus = Literal[
    "diagnosing",          # gathering evidence / generating hypotheses
    "awaiting_clarification",
    "awaiting_plan_approval",
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
    token_usage: int

    # --- Escalation loop ---
    escalation_round: int
    completion_forecast_pct: Optional[float]

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
        plans=[], decision=None, approved_plan=None,
        human_plan_feedback=None, plan_revision_round=0, max_plan_revision_rounds=max_plan_revision_rounds,
        committed_actions={}, token_usage=0,
        escalation_round=0, completion_forecast_pct=None,
        status="diagnosing", error=None,
    )
