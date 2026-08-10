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
    # "risk_signal" is a deterministic RiskSignal restated as citable evidence — see diagnose_node's
    # comment on why signals have to be citable, not just described in the prompt.
    source_type: Literal["comment", "attachment", "history", "description", "structured", "risk_signal"]
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
    # **Found live, from direct product feedback**: a human rewound with "we will add 2 extra engineers
    # to this sprint" and got back plans that said nothing about engineers. The note *was* used (it
    # became evidence, and a hypothesis weighed it explicitly and explained why extra capacity can't
    # move work blocked on an external party) — but nothing in the plan output acknowledged it, so the
    # only visible outcome was indistinguishable from having been ignored. Telling someone *why* their
    # input didn't change the answer is part of the interaction, not an optional extra: silence reads
    # as "not listening." Only requested when there is actually an unanswered note — see plan_node.
    response_to_human_note: Optional[str] = Field(
        None,
        description=(
            "Direct reply to what the human just told you: what it changed about these plans, or the "
            "concrete reason it could not change them."
        ),
    )


class SprintIssueSummary(BaseModel):
    """One issue's structural facts, carried forward from risk detection so later nodes can reason
    about the sprint as a whole.

    **Found live**: `_detect_risk_signals` already queried every issue in the sprint (status, owner,
    points, priority) but returned only the signals it derived, throwing the rest away — so
    `plan_node` could only ever act on issues that had independently tripped a per-issue signal.
    That's why a sprint-level problem ("nobody has started anything") had no ticket to act on: not
    because the data was missing, but because it wasn't passed along.
    """

    issue_key: str
    title: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee_name: Optional[str] = None
    story_points: Optional[int] = None


class SprintSnapshot(BaseModel):
    """The whole sprint's shape at diagnosis time — the context a human applies automatically when
    judging risk, which the workflow previously never had."""

    sprint_name: Optional[str] = None
    goal: Optional[str] = None
    days_remaining: Optional[int] = None
    elapsed_percent: Optional[int] = None
    issues: List[SprintIssueSummary] = Field(default_factory=list)


