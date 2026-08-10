"""Structured output schema for AI-assisted epic planning.

Same `instructor`-validated-Pydantic-model-is-the-API-contract approach as
`app/drafting/schemas.py::TaskDraft`, scaled up to a whole epic: one LLM call produces both the epic
and its decomposed issues together (not two separate calls) since the issues are derived directly
from the epic's goals in the same reasoning pass — splitting it into two calls would just add latency
without adding independence between the two outputs.

`depends_on` deliberately references other issues' `temp_id` (scoped to this response only), never an
issue key — the LLM has no real issue keys to reference yet, nothing is persisted until the human
reviews and confirms the whole plan.

That "one call, not two" claim above was put under test rather than left as an assumption:
`app/planning/graph.py` briefly split estimation into its own node and
`eval/planning_multiagent_eval.py` measured it. The split produced no improvement at all (delta 0.00),
so the node was deleted and this docstring's original reasoning stands — now with a number behind it.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class EpicDraft(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    description: str = Field(
        ..., description="A few sentences summarizing the epic's scope and intent."
    )
    goals: List[str] = Field(
        default_factory=list,
        description="Concrete outcomes this epic should achieve, one per bullet.",
    )


class IssueDraft(BaseModel):
    temp_id: str = Field(
        ..., description="A small identifier unique within this response only (e.g. '1', '2'), used "
                          "to express dependencies via `depends_on` — never a real issue key."
    )
    title: str = Field(
        ..., min_length=1, max_length=120,
        description="Concise, actionable issue title in imperative mood.",
    )
    description: str = Field(
        ..., min_length=1,
        description="1-3 sentences describing what needs to be done and why — enough for someone to "
                    "start work without re-reading the whole epic.",
    )
    issue_type: Literal["story", "task", "bug"] = Field(
        "task", description="story = user-facing, value-delivering work a stakeholder would recognize "
                             "as 'a feature landed'. task = internal/technical/enabling work with no "
                             "direct user-visible outcome by itself (infra, wiring, config, refactors). "
                             "bug = fixing a defect in existing behavior — rare in a net-new epic unless "
                             "the proposal explicitly describes fixing something broken.",
    )
    labels: List[str] = Field(
        default_factory=list,
        description="Relevant labels, lowercase, only if clearly implied — an empty list is correct "
                    "when nothing is clearly implied.",
    )
    estimate_story_points: Optional[int] = Field(
        None, ge=1, le=13,
        description="A rough Fibonacci-like estimate (1, 2, 3, 5, 8, 13), only if there's enough "
                    "signal to guess complexity. Null is correct when it's genuinely unclear.",
    )
    estimate_rationale: Optional[str] = Field(
        None, description="One short sentence explaining what makes this estimate that size (scope, "
                           "unknowns, dependencies, etc). Null if and only if estimate_story_points is "
                           "null.",
    )
    depends_on: List[str] = Field(
        default_factory=list,
        description="temp_ids of other issues in this same response that this issue can only start "
                    "after — only if the decomposition genuinely requires an ordering, never invented "
                    "just to show structure.",
    )


class EpicPlanDraft(BaseModel):
    epic: EpicDraft
    issues: List[IssueDraft] = Field(
        ..., min_length=1,
        description="A decomposition of the epic into concrete, independently workable issues — "
                    "typically 3 to 12 depending on the proposal's scope.",
    )


# --- Multi-agent planning graph (app/planning/graph.py) schemas ---
#
# These back the planner -> critic pipeline, an alternative to the single-call `plan_epic` above.
# `EpicDraft`/`IssueDraft`/`EpicPlanDraft` are reused as-is by both nodes — the planner emits an
# `EpicPlanDraft` exactly as before, so only the critic's own output needs a new shape.


class UnrealisticEstimate(BaseModel):
    temp_id: str = Field(..., description="The issue whose estimate looks off.")
    reason: str = Field(..., description="Why this estimate looks too small or too large for the work described.")


class MissingDependency(BaseModel):
    temp_id: str = Field(..., description="The issue that is likely missing a depends_on edge.")
    likely_depends_on: str = Field(..., description="The other issue's temp_id it probably needs to wait on.")
    reason: str = Field(..., description="Why this ordering is likely required.")


class PlanCritique(BaseModel):
    """Evidence only — deliberately has no self-reported verdict field (e.g. `approved: bool`). A
    model asked to both list problems AND declare a verdict can produce an internally inconsistent
    answer (a real gap listed, verdict still "approved"). Whether the plan needs another revision
    round is instead computed from these lists by `app.planning.service.plan_needs_revision`, a plain,
    independently unit-tested function — the same "LLM lists evidence, code computes the verdict"
    split `app.sprint_pace.service`'s risk-level handling already uses.

    Every list field follows this codebase's existing Optional/List convention throughout
    `IssueDraft`: an empty list is a correct, positive statement that nothing of that kind was found —
    never invent a problem just to appear thorough.
    """

    coverage_gaps: List[str] = Field(
        default_factory=list,
        description="Concrete, missing pieces of work the proposal implies but the plan doesn't cover. "
                    "Empty is correct when the decomposition is genuinely complete.",
    )
    redundant_issue_groups: List[List[str]] = Field(
        default_factory=list,
        description="Groups (by temp_id) of issues that substantially overlap and should likely be "
                    "merged. Empty is correct when there's no real overlap.",
    )
    unrealistic_estimates: List[UnrealisticEstimate] = Field(
        default_factory=list,
        description="Estimates that look clearly too small or too large for the described scope. "
                    "Empty is correct when every estimate looks reasonable (or is null).",
    )
    missing_dependencies: List[MissingDependency] = Field(
        default_factory=list,
        description="Issue pairs that are very likely to need a depends_on edge the plan doesn't "
                    "have. Empty is correct when the ordering already given looks right.",
    )
