"""Structured-output schemas for the sprint-recovery workflow (app/sprint_recovery/graph.py).

Same `instructor`-validated-Pydantic-model-is-the-API-contract approach as `app/planning/schemas.py`.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    """One retrieved fact, tagged with a stable `citation_id` so hypotheses can reference it and a
    grounding check (mirroring `crag_loop.py`'s `_ground_answer`) can verify every claim traces back
    to something actually retrieved, not invented.
    """

    citation_id: str = Field(..., description="Stable id within this workflow, e.g. 'ev1', 'ev2'.")
    issue_key: str
    source_type: Literal["comment", "attachment", "history", "description", "structured"]
    content: str


class RiskSignal(BaseModel):
    """A *deterministic* fact about the sprint — computed by code (query_issues/query_issue_history
    results), never guessed by the model. This is the "code computes, LLM explains" split every other
    feature in this codebase already uses (`sprint_health`, `PlanCritique`), applied here."""

    issue_key: Optional[str] = None
    signal_type: Literal[
        "blocked_no_flag", "long_in_progress", "owner_overloaded", "no_dependency_recorded",
        "scope_added_late", "low_completion_forecast",
    ]
    description: str


class RootCauseHypothesis(BaseModel):
    statement: str = Field(..., description="A specific, falsifiable claim about why the sprint is at risk.")
    confidence: Literal["high", "medium", "low"]
    supporting_evidence_ids: List[str] = Field(
        default_factory=list,
        description="citation_id values from the evidence actually retrieved this turn. A hypothesis "
                    "with no supporting_evidence_ids is rejected by the grounding check, not trusted "
                    "on the model's word alone.",
    )


class DiagnosisResult(BaseModel):
    hypotheses: List[RootCauseHypothesis]
    overall_confidence: Literal["sufficient", "insufficient"] = Field(
        ..., description="'insufficient' only if the evidence gathered genuinely leaves a material "
                          "question unanswered — not a default hedge.",
    )
    clarifying_question: Optional[str] = Field(
        None, description="Set if and only if overall_confidence='insufficient' — one specific "
                           "question a human could answer to resolve the gap.",
    )


class RecoveryAction(BaseModel):
    action_type: Literal["link_dependency", "change_priority", "move_out_of_sprint", "add_comment"]
    target_issue_key: str
    # Action-specific parameters, all optional since each action_type only uses a subset.
    depends_on_issue_key: Optional[str] = Field(None, description="For link_dependency: the blocking issue.")
    new_priority: Optional[str] = Field(None, description="For change_priority: e.g. 'highest'.")
    comment_body: Optional[str] = Field(None, description="For add_comment: the comment text.")


class RecoveryPlan(BaseModel):
    plan_id: str = Field(..., description="Short stable id, e.g. 'plan_a'.")
    name: str
    rationale: str
    impact_on_goal: str = Field(..., description="One sentence: what this plan costs/saves toward the sprint goal.")
    actions: List[RecoveryAction]


class RecoveryPlanSet(BaseModel):
    plans: List[RecoveryPlan] = Field(..., min_length=1, max_length=3)
