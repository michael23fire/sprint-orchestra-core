"""Tests for the epic-rollout durable workflow (app/planning/rollout_graph.py).

Fast unit tests below use `InMemorySaver` (routing/idempotency logic doesn't depend on which
checkpointer backend is used). `test_crash_mid_commit_resumes_without_duplicate_writes` is the one
exception — it deliberately uses a *real* `AsyncPostgresSaver` against the dev Postgres, because the
thing being proven (state survives a genuine process boundary) cannot be demonstrated by an in-memory
object that trivially survives because it was never destroyed. Skipped automatically if that Postgres
isn't reachable, same shape `tests/test_pipeline_idempotency.py`-style integration tests elsewhere in
this project already use for optional real-infra dependencies.
"""
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.auth.space_membership import NoopSpaceMembershipChecker, SpaceMembershipError
from app.planning.jira_commit_client import JiraCommitError
from app.planning.rollout_graph import build_rollout_graph, retry_rollout
from app.planning.rollout_schemas import initial_rollout_state
from app.planning.schemas import EpicDraft, EpicPlanDraft, IssueDraft
from tests.test_planning import FakeInstructorClient

_EPIC = EpicDraft(title="Add dark mode", description="Support a dark theme app-wide.", goals=[])


def _issue(temp_id, title=None, points=3, depends_on=None):
    return IssueDraft(
        temp_id=temp_id, title=title or f"Issue {temp_id}", description=f"Description {temp_id}",
        issue_type="task", labels=[], estimate_story_points=points, estimate_rationale="sized",
        depends_on=depends_on or [],
    )


class FakeJiraCommitClient:
    """One shared call ledger across `create_epic_issue` + `create_issue` (the epic is always call 1,
    since `create_epic_node` runs before any `commit_one_node`) — `fail_on_call` counts across both,
    matching how a real crash could land on either. Can be told to raise a `_SimulatedCrash` (not
    caught by the nodes' `except JiraCommitError`) to model the process dying mid-write.
    """

    def __init__(self, fail_on_call: int = None, raise_type=JiraCommitError):
        self.calls = []
        self._fail_on_call = fail_on_call
        self._raise_type = raise_type
        self._next_key = 100
        self._next_sprint_id = 500
        self._next_link_id = 900

    def _maybe_fail(self):
        if self._fail_on_call is not None and len(self.calls) == self._fail_on_call:
            raise self._raise_type(f"simulated failure on call {len(self.calls)}")

    async def create_epic_issue(self, space_id, epic, user_id, username):
        self.calls.append(("epic", epic.title))
        self._maybe_fail()
        self._next_key += 1
        return self._next_key, f"ATC-{self._next_key}"  # (issue_id, issue_key)

    async def create_sprint(self, space_id, name, user_id, username):
        self.calls.append(("sprint", name, user_id, username))
        self._maybe_fail()
        self._next_sprint_id += 1
        return self._next_sprint_id

    async def create_issue(self, space_id, issue, parent_id, sprint_id, user_id, username):
        self.calls.append(("issue", issue.temp_id, parent_id, sprint_id, user_id, username))
        self._maybe_fail()
        self._next_key += 1
        return self._next_key, f"ATC-{self._next_key}"

    async def create_issue_link(self, source_issue_id, target_issue_key, user_id, username):
        self.calls.append(("link", source_issue_id, target_issue_key, user_id, username))
        self._maybe_fail()
        self._next_link_id += 1
        return self._next_link_id


def _graph(client, jira, space_membership=None, planning_graph=None):
    return build_rollout_graph(
        client, "fake-model", space_membership or NoopSpaceMembershipChecker(), jira,
        lambda: planning_graph,
    ).compile(checkpointer=InMemorySaver())


def _state(space_id=5000014, user_id="u1", username="alice"):
    return initial_rollout_state("Add dark mode", [], None, None, space_id, user_id, username)


def _cfg():
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


async def test_happy_path_approve_creates_epic_then_every_issue_parent_linked_to_it():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2"), _issue("3")])
    client = FakeInstructorClient(plan)
    jira = FakeJiraCommitClient()
    graph = _graph(client, jira)
    cfg = _cfg()

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert "__interrupt__" in r1
    assert r1["status"] == "pending_approval"

    r2 = await graph.ainvoke(Command(resume={"decision": "approve"}), config=cfg)
    assert r2["status"] == "committed"
    assert r2["epic_issue_key"] == "ATC-101"
    assert r2["committed"] == {"1": "ATC-102", "2": "ATC-103", "3": "ATC-104"}
    # Call order: epic first, then issues in plan order — and every issue call carries the epic's
    # real jira-backend id (101) as parent_id, not a synthetic label.
    assert jira.calls[0] == ("epic", "Add dark mode")
    assert [c[1] for c in jira.calls[1:]] == ["1", "2", "3"]
    assert {c[2] for c in jira.calls[1:]} == {101}


async def test_reject_never_calls_jira():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1")])
    client = FakeInstructorClient(plan)
    jira = FakeJiraCommitClient()
    graph = _graph(client, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "reject"}), config=cfg)

    assert result["status"] == "rejected"
    assert jira.calls == []


async def test_edit_commits_the_caller_supplied_plan_not_the_generated_one():
    generated = EpicPlanDraft(epic=_EPIC, issues=[_issue("1", title="Generated")])
    client = FakeInstructorClient(generated)
    jira = FakeJiraCommitClient()
    graph = _graph(client, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    edited_epic = EpicDraft(title="Add dark mode (edited)", description="Edited scope.", goals=[])
    edited_issue = _issue("1", title="Human-edited title")
    result = await graph.ainvoke(
        Command(resume={
            "decision": "edit",
            "epic": edited_epic.model_dump(),
            "issues": [edited_issue.model_dump()],
        }),
        config=cfg,
    )

    assert result["status"] == "committed"
    # The epic actually created is the *edited* one, not the originally generated title.
    assert jira.calls[0] == ("epic", "Add dark mode (edited)")
    assert jira.calls[1][1] == "1"


async def test_edit_publishes_new_and_existing_sprints_and_dependency_links():
    generated = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2", depends_on=["1"])])
    jira = FakeJiraCommitClient()
    graph = _graph(FakeInstructorClient(generated), jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(
        Command(resume={
            "decision": "edit",
            "epic": _EPIC.model_dump(),
            "issues": [i.model_dump() for i in generated.issues],
            "sprint_targets": [
                {
                    "sprint_index": 0,
                    "issue_temp_ids": ["1"],
                    "mode": "new",
                    "sprint_id": None,
                    "sprint_name": "Dark mode — Sprint 1",
                },
                {
                    "sprint_index": 1,
                    "issue_temp_ids": ["2"],
                    "mode": "existing",
                    "sprint_id": 777,
                    "sprint_name": None,
                },
            ],
        }),
        config=cfg,
    )

    assert result["status"] == "committed"
    assert result["created_sprints"] == {"0": 501, "1": 777}
    assert result["committed_links"] == {"2>1": 901}
    assert jira.calls == [
        ("epic", "Add dark mode"),
        ("sprint", "Dark mode — Sprint 1", "u1", "alice"),
        ("issue", "1", 101, 501, "u1", "alice"),
        ("issue", "2", 101, 777, "u1", "alice"),
        ("link", 103, "ATC-102", "u1", "alice"),
    ]


async def test_rbac_recheck_failure_at_resume_rejects_without_committing():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1")])
    client = FakeInstructorClient(plan)
    jira = FakeJiraCommitClient()

    class _AlwaysDenies:
        async def validate(self, user_id, username, space_ids):
            raise SpaceMembershipError(user_id, space_ids)

    graph = _graph(client, jira, space_membership=_AlwaysDenies())
    cfg = _cfg()
    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "approve"}), config=cfg)

    assert result["status"] == "rejected"
    assert jira.calls == []


async def test_epic_creation_failure_never_reaches_commit_one():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2")])
    client = FakeInstructorClient(plan)
    jira = FakeJiraCommitClient(fail_on_call=1)  # the epic call itself fails
    graph = _graph(client, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "approve"}), config=cfg)

    assert result["status"] == "failed"
    assert result["committed"] == {}
    assert result["epic_issue_id"] is None
    assert len(jira.calls) == 1  # never got as far as attempting an issue


async def test_jira_failure_mid_commit_stops_with_partial_ledger_preserved():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2"), _issue("3")])
    client = FakeInstructorClient(plan)
    jira = FakeJiraCommitClient(fail_on_call=3)  # epic(1) + issue "1"(2) succeed; issue "2"(3) fails
    graph = _graph(client, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "approve"}), config=cfg)

    assert result["status"] == "failed"
    assert result["epic_issue_key"] == "ATC-101"
    assert result["committed"] == {"1": "ATC-102"}  # issue 1 committed and preserved; 2/3 never attempted after the failure
    assert result["error"]


async def test_aget_state_alone_never_advances_a_crashed_thread_but_retry_rollout_does():
    """Directly tests the gap a plain 'refresh status' click cannot close: after a real crash (the
    node raises instead of returning — nothing caught, unlike the graceful-failure tests above), the
    checkpoint is left at status='committing' with a pending task. Repeated `aget_state` reads (what
    `GET /plan-epic/rollout/{id}` actually does) must never change that on their own; only
    `retry_rollout` (a real `ainvoke`) may.
    """
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2")])
    client = FakeInstructorClient(plan)

    class _SimulatedCrash(Exception):
        pass

    jira = FakeJiraCommitClient(fail_on_call=3, raise_type=_SimulatedCrash)  # epic(1)+issue1(2) ok; issue2(3) "crashes"
    graph = _graph(client, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    with pytest.raises(_SimulatedCrash):
        await graph.ainvoke(Command(resume={"decision": "approve"}), config=cfg)

    for _ in range(3):
        snap = await graph.aget_state(cfg)
        assert snap.values["status"] == "committing"
        assert snap.values["committed"] == {"1": "ATC-102"}  # never changes across repeated reads

    result = await retry_rollout(graph, cfg["configurable"]["thread_id"])
    assert result["status"] == "committed"
    assert result["committed"] == {"1": "ATC-102", "2": "ATC-103"}


async def test_retry_after_a_clean_failure_resumes_without_recreating_committed_issues():
    """The 'jira-backend was briefly unreachable, ai-service stayed alive' case — a *caught* error,
    not a crash. The graph runs to a normal END with status='failed'; a bare re-`ainvoke(None, ...)`
    would be a no-op (nothing pending), which is exactly why `retry_rollout` exists.
    """
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2"), _issue("3")])
    client = FakeInstructorClient(plan)
    jira = FakeJiraCommitClient(fail_on_call=3)  # epic(1) + issue "1"(2) succeed; issue "2"(3) fails once
    graph = _graph(client, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    failed = await graph.ainvoke(Command(resume={"decision": "approve"}), config=cfg)
    assert failed["status"] == "failed"
    assert failed["committed"] == {"1": "ATC-102"}

    # A bare re-invoke does nothing — the graph already reached END normally, nothing is pending.
    noop = await graph.ainvoke(None, config=cfg)
    assert noop["status"] == "failed"
    assert len(jira.calls) == 3

    result = await retry_rollout(graph, thread_id)
    assert result["status"] == "committed"
    assert result["committed"] == {"1": "ATC-102", "2": "ATC-103", "3": "ATC-104"}
    # Issue "1" was never re-requested — only issue "2" (retried) and "3" (first attempt) fired.
    assert [c[1] for c in jira.calls[3:]] == ["2", "3"]


async def test_retry_when_the_epic_itself_never_got_created():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1")])
    client = FakeInstructorClient(plan)
    jira = FakeJiraCommitClient(fail_on_call=1)  # the epic call itself fails once
    graph = _graph(client, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    failed = await graph.ainvoke(Command(resume={"decision": "approve"}), config=cfg)
    assert failed["status"] == "failed"
    assert failed["epic_issue_id"] is None

    result = await retry_rollout(graph, thread_id)
    assert result["status"] == "committed"
    # The failed first attempt never consumed a key; the retry's successful call got ATC-101.
    assert result["epic_issue_key"] == "ATC-101"
    assert result["committed"] == {"1": "ATC-102"}
    assert [c[0] for c in jira.calls] == ["epic", "epic", "issue"]  # first epic attempt, retry, then the issue


async def test_retry_after_new_sprint_failure_does_not_recreate_epic():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1")])
    jira = FakeJiraCommitClient(fail_on_call=2)  # epic succeeds, first sprint create fails
    graph = _graph(FakeInstructorClient(plan), jira)
    cfg = _cfg()
    await graph.ainvoke(_state(), config=cfg)

    failed = await graph.ainvoke(Command(resume={
        "decision": "edit",
        "epic": _EPIC.model_dump(),
        "issues": [plan.issues[0].model_dump()],
        "sprint_targets": [{
            "sprint_index": 0,
            "issue_temp_ids": ["1"],
            "mode": "new",
            "sprint_id": None,
            "sprint_name": "Dark mode — Sprint 1",
        }],
    }), config=cfg)
    assert failed["status"] == "failed"
    assert failed["failed_step"] == "prepare_sprint"

    result = await retry_rollout(graph, cfg["configurable"]["thread_id"])
    assert result["status"] == "committed"
    assert [call[0] for call in jira.calls] == ["epic", "sprint", "sprint", "issue"]


async def test_retry_after_dependency_failure_only_retries_the_link():
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2", depends_on=["1"])])
    jira = FakeJiraCommitClient(fail_on_call=4)  # epic + 2 issues succeed; dependency link fails
    graph = _graph(FakeInstructorClient(plan), jira)
    cfg = _cfg()
    await graph.ainvoke(_state(), config=cfg)

    failed = await graph.ainvoke(Command(resume={
        "decision": "edit",
        "epic": _EPIC.model_dump(),
        "issues": [i.model_dump() for i in plan.issues],
        "sprint_targets": [{
            "sprint_index": 0,
            "issue_temp_ids": ["1", "2"],
            "mode": "existing",
            "sprint_id": 777,
            "sprint_name": None,
        }],
    }), config=cfg)
    assert failed["status"] == "failed"
    assert failed["failed_step"] == "link_one"
    assert failed["committed"] == {"1": "ATC-102", "2": "ATC-103"}

    result = await retry_rollout(graph, cfg["configurable"]["thread_id"])
    assert result["status"] == "committed"
    assert result["committed_links"] == {"2>1": 901}
    assert [call[0] for call in jira.calls] == ["epic", "issue", "issue", "link", "link"]


async def test_crash_mid_commit_resumes_without_duplicate_writes():
    """The actual fault-injection test: kill the process after the epic + 2 of 3 issues are durably
    committed, resume from a brand-new checkpointer connection + a brand-new graph object (nothing
    shared with the pre-crash run except the Postgres row and the thread_id), and confirm the third
    issue is created exactly once — not zero times, not twice, and the epic is never recreated either.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = await psycopg.AsyncConnection.connect(
            "postgresql://poc:poc123@localhost:5432/pocdb", connect_timeout=2,
        )
        await conn.close()
    except Exception:
        pytest.skip("dev Postgres (poc-postgres) not reachable")

    from app.planning.checkpoint import build_checkpointer
    from contextlib import AsyncExitStack

    db_url = "postgresql://poc:poc123@localhost:5432/pocdb"
    plan = EpicPlanDraft(epic=_EPIC, issues=[_issue("1"), _issue("2"), _issue("3")])
    thread_id = str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": thread_id}}

    class _SimulatedCrash(Exception):
        pass

    # --- "process 1": plans, gets approved, creates the epic + issue 1 + issue 2, crashes on issue 3
    # (call order: epic=1, issue1=2, issue2=3, issue3=4 -> fail_on_call=4). ---
    stack1 = AsyncExitStack()
    try:
        checkpointer1 = await build_checkpointer(db_url, stack1)
        client1 = FakeInstructorClient(plan)
        jira1 = FakeJiraCommitClient(fail_on_call=4, raise_type=_SimulatedCrash)
        graph1 = build_rollout_graph(
            client1, "fake-model", NoopSpaceMembershipChecker(), jira1, lambda: None,
        ).compile(checkpointer=checkpointer1)

        await graph1.ainvoke(_state(), config=cfg)
        with pytest.raises(_SimulatedCrash):
            await graph1.ainvoke(Command(resume={"decision": "approve"}), config=cfg)

        mid_state = await graph1.aget_state(cfg)
        assert mid_state.values["epic_issue_key"] == "ATC-101"
        assert mid_state.values["committed"] == {"1": "ATC-102", "2": "ATC-103"}
        assert mid_state.values["status"] == "committing"
    finally:
        await stack1.aclose()

    # --- "process 2": brand-new connection, brand-new graph/client objects, same thread_id. ---
    stack2 = AsyncExitStack()
    try:
        checkpointer2 = await build_checkpointer(db_url, stack2)
        jira2 = FakeJiraCommitClient()  # fresh call ledger — proves epic/1/2 are NOT re-requested
        graph2 = build_rollout_graph(
            FakeInstructorClient(plan), "fake-model", NoopSpaceMembershipChecker(), jira2, lambda: None,
        ).compile(checkpointer=checkpointer2)

        # Re-invoking (not resuming with a new decision) continues the in-flight commit loop from
        # its last checkpoint — the interrupt was already answered before the crash.
        final = await graph2.ainvoke(None, config=cfg)

        assert final["status"] == "committed"
        # Epic and issues 1/2 keep their pre-crash keys (never re-requested); issue 3 gets jira2's
        # *first* assigned key (ATC-101, not ATC-104) — proof this is a fresh client ledger.
        assert final["epic_issue_key"] == "ATC-101"
        assert final["committed"] == {"1": "ATC-102", "2": "ATC-103", "3": "ATC-101"}
        # The critical assertion: process 2 only ever asked jira-backend to create issue "3" — no
        # epic-recreation call, no re-request of issues 1/2.
        assert jira2.calls == [("issue", "3", final["epic_issue_id"], None, "u1", "alice")]
    finally:
        await stack2.aclose()
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            await conn.execute(
                "DELETE FROM planning_workflows.checkpoints WHERE thread_id = %s", (thread_id,)
            )
            await conn.execute(
                "DELETE FROM planning_workflows.checkpoint_writes WHERE thread_id = %s", (thread_id,)
            )
            await conn.execute(
                "DELETE FROM planning_workflows.checkpoint_blobs WHERE thread_id = %s", (thread_id,)
            )
