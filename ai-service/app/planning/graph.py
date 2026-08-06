"""Multi-agent epic planning: planner -> critic, with a bounded revision loop, wired as a LangGraph
`StateGraph`. An alternative to `app/planning/service.py`'s single-call `plan_epic`, selected
per-deployment by `Settings.epic_planning_multiagent_enabled` (off by default).

**Why LangGraph here, stated honestly rather than assumed.** `app/agent/crag_loop.py`'s
`CragAgent.ask()` already runs a bounded, conditional retrieve-grade-retry loop with a plain Python
`for` loop and no graph library at all — so the *control flow* of this feature does not require
LangGraph, and claiming otherwise would be dishonest. The reasons it's used anyway are narrower and
real:
  1. The routing rules become data (`add_conditional_edges` maps a decision function's return value
     to a next node) instead of `if`/`break` statements interleaved with the work each step does.
     With three distinct decision points, that separation is worth something; with `crag_loop`'s one,
     it wasn't.
  2. Per-node transition events come for free via `astream_events` — verified against this graph, not
     assumed from documentation: an `on_chain_start` event fires named "planner"/"critic" as each runs. `crag_loop.py` had to hand-thread an `on_stage` callback through every call site to
     get the equivalent. Nothing consumes these yet; `/plan-epic` is a blocking endpoint.
  3. It's additive and isolated: pure orchestration over the *same* `instructor` client every other
     structured-output feature in this codebase already uses (see
     `app/drafting/instructor_client.py`), not a second LLM abstraction and not a framework migration.

**What is deliberately NOT here.** No checkpointer / cross-request persistence: every request is a
complete plan produced from scratch, and the frontend already round-trips the whole plan state on
each `/plan-epic/refine` call, so persisting graph state server-side would add a lifecycle to manage
for no behaviour the API needs. And `refine_plan` (the human-instruction endpoint) stays entirely
outside this graph — a small "add a QA task" edit does not need a three-node pipeline plus a review
loop, and routing it through one would multiply its latency for no gain.

**Concurrency.** `build_planning_graph` compiles once at startup and the compiled graph is shared
across all concurrent requests. That's safe because per-request data flows through `ainvoke`'s state
argument rather than instance attributes — the same reasoning `crag_loop.py` documents for its own
shared-agent/`ContextVar` design. `client` and `model` are bound into the node closures rather than
carried in state, since they're process-wide and identical for every request.

**This was a three-node graph, and the middle node was deleted after measuring it.** The original
design put a dedicated `estimator_node` between the planner and the critic: the planner produced an
unestimated decomposition, and a separate batched call assigned story points. The stated hypothesis
was that a focused pass would size issues better than a planner doing it as a side-effect of
decomposition. `eval/planning_multiagent_eval.py` scored `estimate_reasonableness` specifically to
test that, with the cut criterion written into the script before the first run. The measured delta was
**0.00** (4.12 vs 4.12 on 8 proposals) — no lift at all for a third of the pipeline's cost. So the node
is gone and the planner estimates again, which is also what `app/planning/schemas.py` argued in the
first place: re-reading issues the planner just wrote adds a call without adding independence.
(A version that *would* be independent — giving an estimator real historical issues and their actual
story points as reference — is a genuinely different design, and a bigger one; see
eval/results/planning_multiagent_comparison.md.)
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.planning.schemas import EpicDraft, EpicPlanDraft, IssueDraft, PlanCritique
from app.planning.service import (
    PlanResult,
    critique_plan,
    critique_to_instruction,
    plan_epic,
    plan_needs_revision,
    refine_plan,
)

logger = logging.getLogger(__name__)

# One revision pass, not several: a round costs two more LLM calls, and a single critique-then-fix
# cycle is the standard actor-critic shape. Kept at 1 for a reason the eval found rather than a guess:
# across 8 seeded proposals the critic requested **zero** revisions, so a larger cap would be budget
# for a path that has not once been observed to fire. See
# eval/results/planning_multiagent_comparison.md.
MAX_REVISION_ROUNDS = 1


class PlanningState(TypedDict):
    """Plain TypedDict, not a Pydantic model: nothing outside this module's own nodes ever writes it,
    so there's no untrusted boundary to validate. This codebase reserves Pydantic for exactly those
    boundaries (LLM output in `schemas.py`, HTTP bodies in `api/routes.py`) and uses plain
    dataclasses/dicts for internal bookkeeping (`PlanResult`, `SprintBucket`, `AgentAnswer`).
    """

    proposal: str
    existing_labels: List[str]
    epic: Optional[EpicDraft]
    issues: List[IssueDraft]
    critique: Optional[PlanCritique]
    needs_revision: bool
    revision_round: int
    degraded: bool
    # "planner_failed" | "critic_failed" | None. Note what is NOT in this list:
    # exhausting MAX_REVISION_ROUNDS. Running out of review budget on a plan nothing actually failed
    # to produce is a different event from a component breaking, and `degraded` is what the frontend
    # keys its "this is a lower-quality fallback" warning on — widening its meaning here would
    # silently change what that banner tells a user.
    degraded_reason: Optional[str]
    error: Optional[str]


def _initial_state(proposal: str, existing_labels: Optional[List[str]]) -> PlanningState:
    return PlanningState(
        proposal=proposal,
        existing_labels=existing_labels or [],
        epic=None,
        issues=[],
        critique=None,
        needs_revision=False,
        revision_round=0,
        degraded=False,
        degraded_reason=None,
        error=None,
    )


async def planner_node(state: PlanningState, client, model: str) -> dict:
    """Round 0 produces a fresh decomposition, estimates included — identical to what the single-call
    path does, since the separate estimator node this graph used to have was measured and cut (see the
    module docstring). Later rounds are revisions, and reuse `refine_plan` verbatim: the critique is
    rendered into the same free-text instruction shape a human reviewer's "also add a QA task" already
    takes (`critique_to_instruction`), so no second planning prompt exists to drift out of sync with
    the refine prompt.
    """
    critique = state.get("critique")
    if critique is None:
        result = await plan_epic(client, model, state["proposal"], state["existing_labels"])
        revision_round = state["revision_round"]
    else:
        result = await refine_plan(
            client,
            model,
            state["epic"],
            state["issues"],
            critique_to_instruction(critique),
            state["existing_labels"],
        )
        revision_round = state["revision_round"] + 1

    update = {
        "epic": result.plan.epic,
        "issues": result.plan.issues,
        "revision_round": revision_round,
        # Consumed — a stale critique here would make the next planner_node call think it's revising
        # again, and would leak into the loop-back condition.
        "critique": None,
        "needs_revision": False,
    }
    if result.degraded:
        update.update(degraded=True, degraded_reason="planner_failed", error=result.error)
    return update


async def critic_node(state: PlanningState, client, model: str) -> dict:
    result = await critique_plan(
        client, model, state["proposal"], state["epic"], state["issues"]
    )
    update = {
        "critique": result.critique,
        "needs_revision": plan_needs_revision(result.critique),
    }
    if result.error is not None:
        update.update(degraded=True, degraded_reason="critic_failed", error=result.error)
    return update


def _after_planner(state: PlanningState) -> str:
    if state["degraded_reason"] == "planner_failed":
        return END  # nothing worth reviewing — `plan_epic`'s fallback is a stub plan
    return "critic"


def _after_critic(state: PlanningState) -> str:
    if state["degraded_reason"] == "critic_failed" or not state["needs_revision"]:
        return END
    if state["revision_round"] >= MAX_REVISION_ROUNDS:
        # Deliberately not `degraded=True` — see PlanningState.degraded_reason's comment.
        logger.info(
            "plan critique still had findings at the revision cap; returning the current plan",
            extra={"revision_round": state["revision_round"]},
        )
        return END
    return "planner"


def build_planning_graph(client, model: str):
    """Compile once at startup (see `app/main.py`) and reuse — model loading isn't the cost here, but
    graph compilation is pure setup that has no business running per request.
    """

    async def _planner(state: PlanningState) -> dict:
        return await planner_node(state, client, model)

    async def _critic(state: PlanningState) -> dict:
        return await critic_node(state, client, model)

    graph = StateGraph(PlanningState)
    graph.add_node("planner", _planner)
    graph.add_node("critic", _critic)

    graph.add_edge(START, "planner")
    graph.add_conditional_edges("planner", _after_planner, {"critic": "critic", END: END})
    graph.add_conditional_edges("critic", _after_critic, {"planner": "planner", END: END})
    return graph.compile()


async def plan_epic_multiagent(
    compiled_graph, proposal: str, existing_labels: Optional[List[str]] = None
) -> PlanResult:
    """Adapter to the exact `PlanResult` shape `plan_epic` returns, so everything downstream —
    `validate_and_order`, `allocate_sprints`, and the `/plan-epic` response assembly — is identical
    regardless of which path produced the plan, and the frontend contract is untouched.
    """
    from app.observability import PLANNING_REVISION_ROUNDS

    start = time.perf_counter()
    final = await compiled_graph.ainvoke(_initial_state(proposal, existing_labels))
    PLANNING_REVISION_ROUNDS.observe(final["revision_round"])
    return PlanResult(
        plan=EpicPlanDraft(epic=final["epic"], issues=final["issues"]),
        degraded=final["degraded"],
        latency_seconds=time.perf_counter() - start,
        error=final["error"],
    )
