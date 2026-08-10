"""AI-assisted epic planning: proposal -> Epic + decomposed Issues + Sprint allocation.

Three-stage pipeline, deliberately split so only the first stage touches the LLM:
  1. `plan_epic` — one `instructor` call -> validated `EpicPlanDraft` (or a safe degraded fallback).
  2. `validate_and_order` — pure, deterministic: breaks any dependency cycles the LLM might have
     produced and returns issues in topological order.
  3. `allocate_sprints` — pure, deterministic: greedy bin-packing of the ordered issues into sprint
     buckets, using real historical velocity (`sprint_capacity_points`, supplied by the caller) rather
     than a guessed number.

Nothing in this module writes to a database — the whole point is to hand back a complete preview the
caller can edit and choose whether to commit, mirroring `app/drafting/service.py`'s "AI drafts, human
approves" shape for a single task, scaled up to a whole epic.

`critique_plan` / `plan_needs_revision` / `critique_to_instruction` below are the
building blocks of the *alternative* multi-agent path (`app/planning/graph.py`, off by default behind
`epic_planning_multiagent_enabled`). They live here rather than in that module on purpose: they are
ordinary async functions with no graph dependency, unit-testable exactly like `plan_epic` is, so the
LangGraph layer is left owning only orchestration.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from app.planning.prompts import (
    render_critic_prompt,
    render_epic_plan_prompt,
    render_epic_plan_refine_prompt,
)
from app.planning.schemas import EpicDraft, EpicPlanDraft, IssueDraft, PlanCritique

logger = logging.getLogger(__name__)

# Same reasoning as app/drafting/service.py's _MAX_VALIDATION_RETRIES / _MAX_OUTPUT_TOKENS: small
# retry budget since this is a synchronous, user-facing call, but generous token headroom since a
# multi-issue decomposition is a meaningfully larger structured output than a single task draft.
_MAX_VALIDATION_RETRIES = 2
_MAX_OUTPUT_TOKENS = 8000
# The critic emits four short evidence lists, not a full epic decomposition — so it gets
# app/sprint_pace/service.py's smaller budget rather than reusing the 8000 above, which was sized
# for the whole plan.
_MAX_REVIEW_OUTPUT_TOKENS = 2000


@dataclass(slots=True)
class PlanResult:
    plan: EpicPlanDraft
    degraded: bool
    latency_seconds: float
    error: Optional[str] = None


@dataclass(slots=True)
class SprintBucket:
    sprint_index: int
    issue_temp_ids: List[str] = field(default_factory=list)
    total_points: int = 0


def _fallback_plan(proposal: str) -> EpicPlanDraft:
    first_line = proposal.strip().splitlines()[0] if proposal.strip() else ""
    title = first_line[:120] or "Untitled epic"
    epic = EpicDraft(title=title, description=proposal.strip() or title, goals=[])
    issue = IssueDraft(
        temp_id="1", title=title, description=proposal.strip() or title, issue_type="task", labels=[],
        estimate_story_points=None, estimate_rationale=None, depends_on=[],
    )
    return EpicPlanDraft(epic=epic, issues=[issue])


def _render_current_plan(epic: EpicDraft, issues: List[IssueDraft]) -> str:
    lines = [f"Epic: {epic.title}", f"Description: {epic.description}"]
    if epic.goals:
        lines.append("Goals: " + "; ".join(epic.goals))
    lines.append("")
    lines.append("Issues:")
    for issue in issues:
        points = issue.estimate_story_points if issue.estimate_story_points is not None else "unestimated"
        dep = f" | depends on: {', '.join(issue.depends_on)}" if issue.depends_on else ""
        lines.append(f"- [{issue.temp_id}] ({issue.issue_type}, {points} pts){dep}: {issue.title}")
        if issue.description:
            lines.append(f"  {issue.description}")
    return "\n".join(lines)


async def plan_epic(
    client, model: str, proposal: str, existing_labels: Optional[List[str]] = None
) -> PlanResult:
    system_prompt = render_epic_plan_prompt(existing_labels)
    start = time.perf_counter()
    try:
        plan = await client.chat.completions.create(
            model=model,
            response_model=EpicPlanDraft,
            max_retries=_MAX_VALIDATION_RETRIES,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": proposal},
            ],
        )
        return PlanResult(plan=plan, degraded=False, latency_seconds=time.perf_counter() - start)
    except Exception as exc:  # noqa: BLE001 - a structured-output failure must degrade, never 500
        logger.warning(
            "epic plan generation failed after retries; falling back to a single-issue plan",
            extra={"error": str(exc)},
        )
        return PlanResult(
            plan=_fallback_plan(proposal),
            degraded=True,
            latency_seconds=time.perf_counter() - start,
            error=str(exc),
        )


async def refine_plan(
    client,
    model: str,
    epic: EpicDraft,
    issues: List[IssueDraft],
    instruction: str,
    existing_labels: Optional[List[str]] = None,
) -> PlanResult:
    """Applies a free-text instruction ("add a QA task", "drop the inventory one", "combine 2 and 3")
    to an already-generated plan, the same conversational back-and-forth a real sprint planning
    session has. Unlike `plan_epic`'s fallback (a fresh single-issue plan — reasonable when there was
    no prior plan to protect), this one's failure fallback is the *unchanged input plan*: a failed
    edit must never destroy a plan the human was already reviewing.
    """
    system_prompt = render_epic_plan_refine_prompt(existing_labels)
    user_message = _render_current_plan(epic, issues) + f"\n\nInstruction: {instruction}"
    start = time.perf_counter()
    try:
        plan = await client.chat.completions.create(
            model=model,
            response_model=EpicPlanDraft,
            max_retries=_MAX_VALIDATION_RETRIES,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return PlanResult(plan=plan, degraded=False, latency_seconds=time.perf_counter() - start)
    except Exception as exc:  # noqa: BLE001 - a failed edit must degrade to a no-op, never destroy the plan
        logger.warning(
            "plan refinement failed after retries; leaving the plan unchanged",
            extra={"error": str(exc)},
        )
        return PlanResult(
            plan=EpicPlanDraft(epic=epic, issues=issues),
            degraded=True,
            latency_seconds=time.perf_counter() - start,
            error=str(exc),
        )


@dataclass(slots=True)
class CritiqueResult:
    critique: PlanCritique
    error: Optional[str] = None


async def critique_plan(
    client, model: str, proposal: str, epic: EpicDraft, issues: List[IssueDraft]
) -> CritiqueResult:
    """Reviews a plan and reports findings only — never edits it. On failure, returns an *empty*
    critique with `error` set, which `plan_needs_revision` reads as "nothing to revise": a broken
    reviewer must not be able to force revision rounds, and it must not block a plan that was
    probably fine.
    """
    user_message = f"Original proposal:\n{proposal}\n\nCurrent plan:\n{_render_current_plan(epic, issues)}"
    try:
        critique = await client.chat.completions.create(
            model=model,
            response_model=PlanCritique,
            max_retries=_MAX_VALIDATION_RETRIES,
            max_tokens=_MAX_REVIEW_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": render_critic_prompt()},
                {"role": "user", "content": user_message},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - a failed review must not block an otherwise fine plan
        logger.warning(
            "plan critique failed after retries; treating the plan as approved",
            extra={"error": str(exc)},
        )
        return CritiqueResult(critique=PlanCritique(), error=str(exc))
    return CritiqueResult(critique=critique)


def plan_needs_revision(critique: PlanCritique) -> bool:
    """The graph's loop-back condition, deliberately computed here in plain Python rather than read
    off a self-reported `approved` field the model sets (see `PlanCritique`'s docstring). Any concrete
    finding in any category is worth one revision pass.
    """
    return bool(
        critique.coverage_gaps
        or critique.redundant_issue_groups
        or critique.unrealistic_estimates
        or critique.missing_dependencies
    )


def critique_to_instruction(critique: PlanCritique) -> str:
    """Renders a critique into the same free-text instruction shape `refine_plan` already consumes,
    so the revision pass reuses the existing refine prompt/template instead of needing a third
    "revise from a critique" prompt that would then have to be kept in sync with it.
    """
    lines: List[str] = []
    for gap in critique.coverage_gaps:
        lines.append(f"- Add work covering a gap in the plan: {gap}")
    for group in critique.redundant_issue_groups:
        if len(group) > 1:
            lines.append(f"- Merge these overlapping issues into one: {', '.join(group)}")
    for item in critique.unrealistic_estimates:
        lines.append(f"- Re-estimate issue {item.temp_id}: {item.reason}")
    for dep in critique.missing_dependencies:
        lines.append(f"- Make issue {dep.temp_id} depend on {dep.likely_depends_on}: {dep.reason}")
    return "A plan review flagged the following. Apply each one:\n" + "\n".join(lines)


def validate_and_order(issues: List[IssueDraft]) -> List[IssueDraft]:
    """Drops dependency edges that would form a cycle (or reference an unknown temp_id) and returns
    issues in topological order. Never raises — a malformed dependency graph from the LLM must degrade
    to *some* valid order, not block the whole plan.
    """
    by_id = {issue.temp_id: issue for issue in issues}
    pending_deps = {
        issue.temp_id: {d for d in issue.depends_on if d in by_id and d != issue.temp_id}
        for issue in issues
    }

    ordered: List[IssueDraft] = []
    remaining = set(by_id.keys())

    while remaining:
        ready = sorted(tid for tid in remaining if not (pending_deps[tid] & remaining))
        if not ready:
            # Cycle: deterministically pick the lowest remaining temp_id and drop its unresolved
            # edges into the remaining set, rather than failing the whole plan over a bad LLM output.
            tid = sorted(remaining)[0]
            dropped = pending_deps[tid] & remaining
            logger.warning("dependency cycle detected; dropping edges from %s to %s", tid, dropped)
            pending_deps[tid] -= dropped
            ready = [tid]
        for tid in ready:
            issue = by_id[tid]
            ordered.append(issue.model_copy(update={"depends_on": sorted(pending_deps[tid])}))
            remaining.discard(tid)

    return ordered


def allocate_sprints(
    ordered_issues: List[IssueDraft],
    sprint_capacity_points: Optional[float],
    target_sprint_count: Optional[int] = None,
) -> List[SprintBucket]:
    """Greedy bin-packing in the given (topological) order — issues with no estimate are placed but
    contribute 0 toward capacity, so they never block packing on a missing number.
    """
    if not ordered_issues:
        return []

    if target_sprint_count and target_sprint_count > 0:
        buckets = [SprintBucket(sprint_index=i) for i in range(target_sprint_count)]
        total_points = sum(issue.estimate_story_points or 0 for issue in ordered_issues)
        target_per_bucket = total_points / target_sprint_count if total_points else 0
        idx = 0
        for issue in ordered_issues:
            points = issue.estimate_story_points or 0
            current = buckets[idx]
            if (
                idx < target_sprint_count - 1
                and current.issue_temp_ids
                and current.total_points >= target_per_bucket
            ):
                idx += 1
                current = buckets[idx]
            current.issue_temp_ids.append(issue.temp_id)
            current.total_points += points
        return [b for b in buckets if b.issue_temp_ids]

    capacity = sprint_capacity_points if sprint_capacity_points and sprint_capacity_points > 0 else None
    buckets = [SprintBucket(sprint_index=0)]
    for issue in ordered_issues:
        points = issue.estimate_story_points or 0
        current = buckets[-1]
        if capacity is not None and current.issue_temp_ids and current.total_points + points > capacity:
            buckets.append(SprintBucket(sprint_index=len(buckets)))
            current = buckets[-1]
        current.issue_temp_ids.append(issue.temp_id)
        current.total_points += points
    return buckets
