"""State shape for the durable epic-rollout workflow (app/planning/rollout_graph.py).

Plain `TypedDict`, not a Pydantic model — same reasoning `PlanningState` in `app/planning/graph.py`
already documents: nothing outside this module's own nodes ever writes it directly (the API layer
only ever supplies the *initial* state or a resume `Command`, both of which go through
`app/api/routes.py`'s own Pydantic request models first), so there's no untrusted boundary here for
Pydantic to validate. It also has to be a plain dict shape: this is exactly what
`AsyncPostgresSaver` serializes into the checkpoint row on every step, and keeping it to primitives +
the existing `EpicDraft`/`IssueDraft` Pydantic models (which `langgraph`'s checkpointer already knows
how to serialize, since `PlanningState` above stores the same types) avoids inventing a second
serialization story for this workflow.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional, TypedDict

from app.planning.schemas import EpicDraft, IssueDraft

RolloutStatus = Literal[
    "pending_approval",  # paused at the interrupt; nothing committed yet
    "committing",         # decision received, commit node is writing to Jira
    "committed",          # every issue in the (possibly edited) plan now has a real issue_key
    "rejected",           # human rejected; nothing was ever written
    "failed",             # plan generation itself failed (see RolloutState.error)
]

RolloutDecision = Literal["approve", "edit", "reject"]


class SprintBucketState(TypedDict):
    sprint_index: int
    issue_temp_ids: List[str]
    total_points: int


class RolloutState(TypedDict):
    # --- Set once, at the start node, never mutated afterward ---
    proposal: str
    existing_labels: List[str]
    sprint_capacity_points: Optional[float]
    target_sprint_count: Optional[int]
    space_id: int
    user_id: str
    username: str

    # --- Written by plan_node; may be re-written by an "edit" decision at resume time ---
    epic: Optional[EpicDraft]
    issues: List[IssueDraft]
    sprint_buckets: List[SprintBucketState]

    # --- Written by the interrupt's resume payload ---
    decision: Optional[RolloutDecision]

    # --- Written once by create_epic_node; checked before every commit_one_node entry (including a
    # post-crash resume) so the epic issue itself is never created twice. ---
    epic_issue_id: Optional[int]
    epic_issue_key: Optional[str]

    # --- Written incrementally by commit_one_node; the idempotency ledger. Keyed by
    # IssueDraft.temp_id. Checked *before* creating each issue on every commit_one_node entry
    # (including a post-crash resume), so an issue already present here is never re-created. ---
    committed: Dict[str, str]  # temp_id -> real issue_key

    # --- Status/error bookkeeping ---
    status: RolloutStatus
    degraded: bool
    error: Optional[str]


def initial_rollout_state(
    proposal: str,
    existing_labels: List[str],
    sprint_capacity_points: Optional[float],
    target_sprint_count: Optional[int],
    space_id: int,
    user_id: str,
    username: str,
) -> RolloutState:
    return RolloutState(
        proposal=proposal,
        existing_labels=existing_labels,
        sprint_capacity_points=sprint_capacity_points,
        target_sprint_count=target_sprint_count,
        space_id=space_id,
        user_id=user_id,
        username=username,
        epic=None,
        issues=[],
        sprint_buckets=[],
        decision=None,
        epic_issue_id=None,
        epic_issue_key=None,
        committed={},
        status="pending_approval",
        degraded=False,
        error=None,
    )
