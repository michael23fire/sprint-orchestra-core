"""Durable Plan Epic: generate -> edit/review -> publish the complete plan to Jira.

The `app/planning/graph.py` planner<->critic graph answers "how is a plan *generated*". This graph
answers a different question this codebase has not needed to answer before: "what happens after a
human is asked to approve something, when that approval might not arrive for hours, and publishing
must resume after a process restart." Durable pause + per-step checkpointed side effects are the
LangGraph capabilities this codebase was missing (see `app/planning/graph.py`'s own docstring on why
its generation loop doesn't need a framework at all).
This graph wraps the *existing* plan-generation call (single-call or multi-agent, whichever
`Settings.epic_planning_multiagent_enabled` selects) rather than re-implementing it.

**Every publish side effect is one graph step.** `AsyncPostgresSaver` checkpoints state between node
executions, so new sprint creation, child-issue creation, and dependency-link creation each advance
one durable ledger entry at a time. A restart resumes from the last recorded step instead of forcing
the browser to reconstruct a partially published plan.

**RBAC is re-checked at resume, not trusted from when the workflow started.** A pending approval can
sit for hours; the approver's space membership is revalidated against jira-backend at the moment the
decision arrives (`review_node`), not assumed still valid from `POST /plan-epic/rollout` time.

**"The checkpoint survives a crash" is not the same claim as "it resumes on its own" — something still
has to call `ainvoke` again.** `aget_state` (what `GET /plan-epic/rollout/{id}`, i.e. the frontend's
"Refresh status" button, calls) is a pure read; it was verified live to leave a crashed thread sitting
at `status="committing"` unchanged across repeated calls, not progressing it. `retry_rollout` (below)
is that explicit trigger, for both stopping-short cases this module has, not only the one below.

**A crash is not the only way this can stop short — a clean failure needs different handling than a
crash does.** If `jira.create_issue`/`create_epic_issue` raises because jira-backend itself is
unreachable (not a process crash — ai-service is alive, the call just failed),
`commit_one_node`/`create_epic_node` catch that and return `status: "failed"` normally. The graph then
finishes a normal run to `END` — there is no pending task for a bare `ainvoke(None, ...)` to pick back
up, unlike the crash case, where the node never returned at all and a pending task *is* already
recorded. `retry_rollout` (below) handles both: a bare re-invoke for `"committing"`, and for `"failed"`
specifically, `aupdate_state(..., as_node=...)` re-arms the correct node (`create_epic` if the epic
itself never got created, `commit_one` otherwise) as if it had just produced a "keep going" status,
then invokes — the same
idempotency ledger (`committed`, `epic_issue_id`) means whatever already succeeded is never re-sent to
jira-backend.

The workflow now replaces the old browser-side publish loop completely: the final human-edited plan,
new-or-existing sprint targets, child issue placement, estimate rationale, and `depends_on` links are
all committed server-side. The standalone rollout UI is therefore unnecessary; this graph is the
publish lifecycle of Plan Epic itself.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Callable, List, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.auth.space_membership import SpaceMembershipChecker, SpaceMembershipError
from app.planning.jira_commit_client import JiraCommitClient, JiraCommitError
from app.planning.rollout_schemas import RolloutState, SprintBucketState, SprintTargetState
from app.planning.schemas import EpicDraft, IssueDraft
from app.planning.service import allocate_sprints, plan_epic, refine_plan, validate_and_order

logger = logging.getLogger(__name__)

# Optional SSE progress-label plumbing — identical mechanism and reasoning to `ON_STAGE_VAR` in
# `app/sprint_recovery/graph.py` (see that module's comment for the full "why a ContextVar, not a
# constructor arg" rationale: `build_rollout_graph` compiles one shared, long-lived graph at startup,
# so per-*request* "does this caller want stage events" has to travel via context). Defined locally
# rather than imported from sprint_recovery — two independent graphs, no shared caller between them,
# not worth a new shared module for one 3-line ContextVar. Only `plan_node` below ever reads it:
# `review`/interrupt is a pause (nothing to wait on), `create_epic`/`commit_one` are fast Jira REST
# writes, not LLM calls worth a stage label.
ON_STAGE_VAR: ContextVar[Optional[Callable[[str], None]]] = ContextVar("rollout_on_stage", default=None)


def _buckets_to_state(buckets) -> List[SprintBucketState]:
    return [
        SprintBucketState(
            sprint_index=b.sprint_index, issue_temp_ids=b.issue_temp_ids, total_points=b.total_points
        )
        for b in buckets
    ]


async def plan_node(state: RolloutState, client, model: str, planning_graph_getter) -> dict:
    """Round 0: produce the initial plan. Reuses whichever plan-generation path is configured — this
    node does not duplicate `POST /plan-epic`'s own dispatch logic, it calls the same functions.
    """
    on_stage = ON_STAGE_VAR.get()
    if on_stage:
        on_stage("generating the rollout plan")
    planning_graph = planning_graph_getter()
    if planning_graph is not None:
        from app.planning.graph import plan_epic_multiagent

        result = await plan_epic_multiagent(planning_graph, state["proposal"], state["existing_labels"])
    else:
        result = await plan_epic(client, model, state["proposal"], state["existing_labels"])

    ordered = validate_and_order(result.plan.issues)
    buckets = allocate_sprints(ordered, state["sprint_capacity_points"], state["target_sprint_count"])
    return {
        "epic": result.plan.epic,
        "issues": ordered,
        "sprint_buckets": _buckets_to_state(buckets),
        "degraded": result.degraded,
        "error": result.error,
        "status": "pending_approval",
    }


def _plan_preview(state: RolloutState) -> dict:
    return {
        "epic": state["epic"].model_dump() if state["epic"] else None,
        "issues": [i.model_dump() for i in state["issues"]],
        "sprint_buckets": state["sprint_buckets"],
    }


async def review_node(state: RolloutState, space_membership: SpaceMembershipChecker) -> dict:
    """Pauses durably (`interrupt`) presenting the current plan, then applies whatever decision the
    resume call supplies. Everything from the `interrupt()` call onward — including the RBAC recheck
    below — only runs *after* a real decision arrives, which may be long after `plan_node` ran.
    """
    decision_payload = interrupt(_plan_preview(state))
    decision = decision_payload.get("decision")

    try:
        await space_membership.validate(state["user_id"], state["username"], [state["space_id"]])
    except SpaceMembershipError as exc:
        logger.warning("rollout RBAC recheck failed at approval time", extra={"error": str(exc)})
        return {"decision": "reject", "status": "rejected", "error": str(exc)}

    if decision == "reject":
        return {"decision": "reject", "status": "rejected"}

    if decision == "edit":
        edited_epic = EpicDraft.model_validate(decision_payload["epic"])
        edited_issues = [IssueDraft.model_validate(i) for i in decision_payload["issues"]]
        ordered = validate_and_order(edited_issues)
        supplied_targets = decision_payload.get("sprint_targets")
        if supplied_targets is None:
            buckets = allocate_sprints(ordered, state["sprint_capacity_points"], state["target_sprint_count"])
            sprint_buckets = _buckets_to_state(buckets)
            sprint_targets: List[SprintTargetState] = []
        else:
            sprint_targets = _validate_sprint_targets(supplied_targets, ordered)
            sprint_buckets = [
                SprintBucketState(
                    sprint_index=t["sprint_index"],
                    issue_temp_ids=t["issue_temp_ids"],
                    total_points=sum(
                        i.estimate_story_points or 0
                        for i in ordered
                        if i.temp_id in t["issue_temp_ids"]
                    ),
                )
                for t in sprint_targets
            ]
        return {
            "decision": "edit",
            "epic": edited_epic,
            "issues": ordered,
            "sprint_buckets": sprint_buckets,
            "sprint_targets": sprint_targets,
            "status": "committing",
            "error": None,
            "failed_step": None,
        }

    return {"decision": "approve", "status": "committing", "error": None, "failed_step": None}


def _validate_sprint_targets(raw_targets: list, issues: List[IssueDraft]) -> List[SprintTargetState]:
    """Validate the browser's final bucket destinations before any Jira write occurs."""
    known_ids = {i.temp_id for i in issues}
    seen_issue_ids: set[str] = set()
    seen_indexes: set[int] = set()
    targets: List[SprintTargetState] = []

    for raw in raw_targets:
        sprint_index = int(raw["sprint_index"])
        if sprint_index in seen_indexes:
            raise ValueError(f"duplicate sprint target index: {sprint_index}")
        seen_indexes.add(sprint_index)
        issue_temp_ids = list(raw.get("issue_temp_ids") or [])
        unknown = set(issue_temp_ids) - known_ids
        duplicate_assignments = set(issue_temp_ids) & seen_issue_ids
        if unknown:
            raise ValueError(f"sprint target references unknown issue temp ids: {sorted(unknown)}")
        if duplicate_assignments:
            raise ValueError(f"issues assigned to more than one sprint: {sorted(duplicate_assignments)}")
        seen_issue_ids.update(issue_temp_ids)

        mode = raw.get("mode")
        sprint_id = raw.get("sprint_id")
        sprint_name = (raw.get("sprint_name") or "").strip() or None
        if mode == "existing":
            if sprint_id is None or int(sprint_id) <= 0:
                raise ValueError("existing sprint target requires a positive sprint_id")
            sprint_id = int(sprint_id)
            sprint_name = None
        elif mode == "new":
            if not sprint_name:
                raise ValueError("new sprint target requires sprint_name")
            sprint_id = None
        else:
            raise ValueError(f"unknown sprint target mode: {mode}")

        if issue_temp_ids:
            targets.append(SprintTargetState(
                sprint_index=sprint_index,
                issue_temp_ids=issue_temp_ids,
                mode=mode,
                sprint_id=sprint_id,
                sprint_name=sprint_name,
            ))

    unassigned = known_ids - seen_issue_ids
    if unassigned:
        raise ValueError(f"issues missing a sprint target: {sorted(unassigned)}")
    return targets


def _after_review(state: RolloutState) -> str:
    return END if state["status"] == "rejected" else "create_epic"


async def create_epic_node(state: RolloutState, jira: JiraCommitClient) -> dict:
    """Creates the real epic-type issue once per recorded workflow state. The ledger check matches
    `commit_one_node`'s: if `epic_issue_id` is already set (a post-crash resume landed here again),
    skip straight past — never create a second epic issue for one rollout.
    """
    if state["epic_issue_id"] is not None:
        return {}
    try:
        issue_id, issue_key = await jira.create_epic_issue(
            state["space_id"], state["epic"], state["user_id"], state["username"]
        )
    except JiraCommitError as exc:
        logger.error("rollout epic creation failed", extra={"error": str(exc)})
        return {"status": "failed", "error": str(exc), "failed_step": "create_epic"}
    return {"epic_issue_id": issue_id, "epic_issue_key": issue_key, "failed_step": None}


def _after_create_epic(state: RolloutState) -> str:
    return END if state["status"] == "failed" else "prepare_sprint"


async def prepare_sprint_node(state: RolloutState, jira: JiraCommitClient) -> dict:
    """Resolve one selected bucket to a real sprint id, checkpointing after each new sprint."""
    resolved = dict(state.get("created_sprints") or {})
    remaining = [
        t for t in state.get("sprint_targets") or []
        if str(t["sprint_index"]) not in resolved
    ]
    if not remaining:
        return {"failed_step": None}

    target = remaining[0]
    try:
        sprint_id = (
            int(target["sprint_id"])
            if target["mode"] == "existing"
            else await jira.create_sprint(
                state["space_id"], target["sprint_name"], state["user_id"], state["username"]
            )
        )
    except JiraCommitError as exc:
        logger.error("rollout sprint creation failed", extra={"error": str(exc)})
        return {"status": "failed", "error": str(exc), "failed_step": "prepare_sprint"}

    resolved[str(target["sprint_index"])] = sprint_id
    return {"created_sprints": resolved, "failed_step": None}


def _after_prepare_sprint(state: RolloutState) -> str:
    if state["status"] == "failed":
        return END
    if len(state.get("created_sprints") or {}) < len(state.get("sprint_targets") or []):
        return "prepare_sprint"
    return "commit_one"


def _sprint_id_for_issue(state: RolloutState, temp_id: str) -> Optional[int]:
    for target in state.get("sprint_targets") or []:
        if temp_id in target["issue_temp_ids"]:
            return (state.get("created_sprints") or {}).get(str(target["sprint_index"]))
    return None


async def commit_one_node(state: RolloutState, jira: JiraCommitClient) -> dict:
    """Commits exactly one not-yet-committed issue, parent-linked to the epic `create_epic_node`
    already created. Idempotency ledger check is the first thing this node does, every time it runs
    (including a post-crash resume) — an issue already in `committed` is never re-requested from
    jira-backend.
    """
    remaining = [i for i in state["issues"] if i.temp_id not in state["committed"]]
    if not remaining:
        return {"status": "committing", "failed_step": None}

    issue = remaining[0]
    try:
        issue_id, issue_key = await jira.create_issue(
            state["space_id"], issue, state["epic_issue_id"],
            _sprint_id_for_issue(state, issue.temp_id), state["user_id"], state["username"]
        )
    except JiraCommitError as exc:
        logger.error("rollout commit failed on temp_id=%s", issue.temp_id, extra={"error": str(exc)})
        return {"status": "failed", "error": str(exc), "failed_step": "commit_one"}

    committed = dict(state["committed"])
    committed[issue.temp_id] = issue_key
    committed_ids = dict(state.get("committed_issue_ids") or {})
    committed_ids[issue.temp_id] = issue_id
    return {
        "committed": committed,
        "committed_issue_ids": committed_ids,
        "status": "committing",
        "failed_step": None,
    }


def _after_commit_one(state: RolloutState) -> str:
    if state["status"] == "failed":
        return END
    return "link_one" if len(state["committed"]) == len(state["issues"]) else "commit_one"


def _dependency_pairs(state: RolloutState) -> list[tuple[str, str]]:
    # A missing/empty target list identifies workflows created by the legacy standalone rollout API,
    # whose contract explicitly did not publish dependencies. Keeping that behavior also lets an
    # in-flight pre-upgrade checkpoint finish without the issue-id ledger added by the integrated UI.
    if not state.get("sprint_targets"):
        return []
    known = {i.temp_id for i in state["issues"]}
    return [
        (issue.temp_id, dependency_id)
        for issue in state["issues"]
        for dependency_id in issue.depends_on
        if dependency_id in known
    ]


async def link_one_node(state: RolloutState, jira: JiraCommitClient) -> dict:
    """Create one dependency link after every issue exists, then checkpoint its link id."""
    committed_links = dict(state.get("committed_links") or {})
    remaining = [
        pair for pair in _dependency_pairs(state)
        if f"{pair[0]}>{pair[1]}" not in committed_links
    ]
    if not remaining:
        return {"status": "committed", "failed_step": None}

    source_temp_id, dependency_temp_id = remaining[0]
    try:
        link_id = await jira.create_issue_link(
            state["committed_issue_ids"][source_temp_id],
            state["committed"][dependency_temp_id],
            state["user_id"],
            state["username"],
        )
    except JiraCommitError as exc:
        logger.error(
            "rollout dependency link failed on %s>%s",
            source_temp_id,
            dependency_temp_id,
            extra={"error": str(exc)},
        )
        return {"status": "failed", "error": str(exc), "failed_step": "link_one"}

    committed_links[f"{source_temp_id}>{dependency_temp_id}"] = link_id
    return {
        "committed_links": committed_links,
        "status": "committing",
        "failed_step": None,
    }


def _after_link_one(state: RolloutState) -> str:
    if state["status"] in ("committed", "failed"):
        return END
    return "link_one"


def build_rollout_graph(
    client,
    model: str,
    space_membership: SpaceMembershipChecker,
    jira: JiraCommitClient,
    planning_graph_getter,
):
    """`planning_graph_getter` is a zero-arg callable (not the graph itself) so this always reads
    `app.state.planning_graph` at call time — the multiagent flag can theoretically differ between
    when this graph is compiled (startup) and when a given rollout runs, and a callable avoids ever
    capturing a stale None/graph reference.
    """

    async def _plan(state: RolloutState) -> dict:
        return await plan_node(state, client, model, planning_graph_getter)

    async def _review(state: RolloutState) -> dict:
        return await review_node(state, space_membership)

    async def _create_epic(state: RolloutState) -> dict:
        return await create_epic_node(state, jira)

    async def _commit_one(state: RolloutState) -> dict:
        return await commit_one_node(state, jira)

    async def _prepare_sprint(state: RolloutState) -> dict:
        return await prepare_sprint_node(state, jira)

    async def _link_one(state: RolloutState) -> dict:
        return await link_one_node(state, jira)

    graph = StateGraph(RolloutState)
    graph.add_node("plan", _plan)
    graph.add_node("review", _review)
    graph.add_node("create_epic", _create_epic)
    graph.add_node("prepare_sprint", _prepare_sprint)
    graph.add_node("commit_one", _commit_one)
    graph.add_node("link_one", _link_one)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "review")
    graph.add_conditional_edges("review", _after_review, {"create_epic": "create_epic", END: END})
    graph.add_conditional_edges(
        "create_epic", _after_create_epic, {"prepare_sprint": "prepare_sprint", END: END}
    )
    graph.add_conditional_edges(
        "prepare_sprint", _after_prepare_sprint,
        {"prepare_sprint": "prepare_sprint", "commit_one": "commit_one", END: END},
    )
    graph.add_conditional_edges(
        "commit_one", _after_commit_one, {"commit_one": "commit_one", "link_one": "link_one", END: END}
    )
    graph.add_conditional_edges("link_one", _after_link_one, {"link_one": "link_one", END: END})
    return graph


async def retry_rollout(compiled_graph, thread_id: str) -> dict:
    """Un-sticks a rollout that isn't `pending_approval`/`committed`/`rejected` — i.e. `committing` or
    `failed`. **Correction from this function's first version**: it assumed a process crash needs no
    explicit call at all, "self-resuming via the checkpointer" the moment anything touches the thread
    again. That is only true of the *checkpointer* — `aget_state` (what `GET /plan-epic/rollout/{id}`,
    i.e. the frontend's "Refresh status", actually calls) is a pure read and never invokes the graph,
    verified live: a thread left mid-crash at `status="committing"` sat there unchanged across three
    repeated `aget_state` calls in a row. Something has to call `ainvoke` again for a crashed run to
    actually continue, and until this function, the HTTP API exposed no way to do that for `committing`
    — only for `failed` (this function's original, narrower scope). Both cases now handled:

    - **`status="committing"`** (a suspected crash — the interrupted node never returned, so the
      checkpointer already has a pending task recorded): a bare `ainvoke(None, ...)` is enough, the
      same mechanism `tests/test_rollout_graph.py`'s crash test already relies on — just now reachable
      through this function/the API instead of only by calling the compiled graph directly in a test.
    - **`status="failed"`** (a *clean* failure — the node returned normally, the graph reached a real
      `END`, nothing is pending): a bare `ainvoke(None, ...)` is a no-op here, which is exactly what
      motivated this function in the first place. Needs `aupdate_state(..., as_node=...)` to re-arm a
      node — and `as_node` names the node whose OUTGOING edge should fire, not the node that reruns,
      verified directly against this langgraph version before relying on it (a 2-node smoke test:
      `as_node="a"` left `a`'s own run count unchanged and incremented `b`'s). So re-arming
      `create_epic` means updating state *as* `review` (whose edge routes to `create_epic` for any
      non-rejected status); re-arming `commit_one` means updating state *as* `create_epic` (whose edge
      routes to `commit_one` for any non-failed status).

    Callers must check `status in ("committing", "failed")` first — calling this on an already-terminal
    `committed`/`rejected` thread, or one still genuinely `pending_approval`, is not meaningful.
    """
    config = {"configurable": {"thread_id": thread_id}}
    snap = await compiled_graph.aget_state(config)
    if snap.values.get("status") == "committing":
        return await compiled_graph.ainvoke(None, config=config)

    failed_step = snap.values.get("failed_step")
    if failed_step is None:
        failed_step = "create_epic" if snap.values.get("epic_issue_id") is None else "commit_one"
    predecessor = {
        "create_epic": "review",
        "prepare_sprint": "create_epic",
        "commit_one": "prepare_sprint",
        "link_one": "commit_one",
    }[failed_step]
    await compiled_graph.aupdate_state(
        config, {"status": "committing", "error": None, "failed_step": None}, as_node=predecessor
    )
    return await compiled_graph.ainvoke(None, config=config)
