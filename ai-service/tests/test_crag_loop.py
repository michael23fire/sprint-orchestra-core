"""CRAG agent loop control-flow tests — scripted fake LLM + fake retrieval, no network.

These test the loop's *decisions* (does it re-retrieve, does it stop, does it respect the security
boundary on space_ids) independent of any real model's answer quality — that's what eval/ measures.
"""
import asyncio

import app.agent.crag_loop as crag_loop
from app.agent.crag_loop import (
    ABSTENTION_PHRASE,
    CragAgent,
    _format_issue_history,
    _initial_tool_choice,
)
from app.agent.retrieval_tool import (
    IssueAttachmentsResult,
    IssueCommentsResult,
    IssueDetailsResult,
    IssueHistoryResult,
    IssueQueryResult,
    SprintQueryResult,
)
from app.llm.types import LLMTurn, ToolCall, Usage


class FakeLLM:
    """Returns each scripted LLMTurn in order, one per call, ignoring the actual input."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0
        self.messages_seen = []  # the `messages` list passed on each call, for assertions

    async def next_turn(self, system, messages, tools, tool_choice=None):
        self.calls += 1
        # Snapshot now: `messages` is the loop's own mutable list, appended to after this call
        # returns (e.g. the assistant turn just produced) — capturing by reference would show
        # future mutations, not what the model actually saw at call time.
        self.messages_seen.append(list(messages))
        self.tool_choices_seen = getattr(self, "tool_choices_seen", [])
        self.tool_choices_seen.append(tool_choice)
        return self._turns.pop(0)

    async def aclose(self):
        return None


class FakeRetrieval:
    def __init__(
        self, results_by_query=None, default=None, issue_result=None, history_result=None,
        sprint_result=None, comments_result=None, details_result=None, attachments_result=None,
    ):
        self._results_by_query = results_by_query or {}
        self._default = default or []
        self._issue_result = issue_result or IssueQueryResult(
            total_count=0, counts_by_type={}, counts_by_status={}, issues=[]
        )
        self._history_result = history_result or IssueHistoryResult(total_count=0, changes=[])
        self._sprint_result = sprint_result or SprintQueryResult(
            total_count=0, counts_by_status={}, sprints=[]
        )
        self._comments_result = comments_result or IssueCommentsResult(total_count=0, comments=[])
        self._details_result = details_result or IssueDetailsResult(total_count=0, details=[])
        self._attachments_result = attachments_result or IssueAttachmentsResult(total_count=0, attachments=[])
        self.calls = []  # (query, space_ids, limit, mode)
        self.issue_calls = []  # (space_ids, filters)
        self.history_calls = []  # (space_ids, filters)
        self.sprint_calls = []  # (space_ids, filters)
        self.comments_calls = []  # (space_ids, issue_keys)
        self.details_calls = []  # (space_ids, issue_keys)
        self.attachments_calls = []  # (space_ids, issue_keys)

    async def search(self, query, space_ids, limit, mode="hybrid"):
        self.calls.append((query, tuple(space_ids), limit, mode))
        return self._results_by_query.get(query, self._default)

    async def query_issues(self, space_ids, filters):
        self.issue_calls.append((tuple(space_ids), dict(filters)))
        return self._issue_result

    async def query_issue_history(self, space_ids, filters):
        self.history_calls.append((tuple(space_ids), dict(filters)))
        return self._history_result

    async def query_sprints(self, space_ids, filters):
        self.sprint_calls.append((tuple(space_ids), dict(filters)))
        return self._sprint_result

    async def get_issue_comments(self, space_ids, issue_keys, limit=200):
        self.comments_calls.append((tuple(space_ids), tuple(issue_keys)))
        return self._comments_result

    async def get_issue_details(self, space_ids, issue_keys, limit=200):
        self.details_calls.append((tuple(space_ids), tuple(issue_keys)))
        return self._details_result

    async def get_issue_attachments(self, space_ids, issue_keys, limit=200):
        self.attachments_calls.append((tuple(space_ids), tuple(issue_keys)))
        return self._attachments_result

    async def aclose(self):
        return None


def _issue_query_turn(tool_input: dict, call_id: str = "iq_1") -> LLMTurn:
    return LLMTurn(
        stop_reason="tool_use",
        text="",
        tool_calls=[ToolCall(id=call_id, name="query_issues", input=tool_input)],
        assistant_message={"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "query_issues", "input": tool_input}
        ]},
    )


def _sprint_query_turn(tool_input: dict, call_id: str = "sq_1") -> LLMTurn:
    return LLMTurn(
        stop_reason="tool_use",
        text="",
        tool_calls=[ToolCall(id=call_id, name="query_sprints", input=tool_input)],
        assistant_message={"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "query_sprints", "input": tool_input}
        ]},
    )


def _issue_comments_turn(tool_input: dict, call_id: str = "ic_1") -> LLMTurn:
    return LLMTurn(
        stop_reason="tool_use",
        text="",
        tool_calls=[ToolCall(id=call_id, name="get_issue_comments", input=tool_input)],
        assistant_message={"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "get_issue_comments", "input": tool_input}
        ]},
    )


def _issue_details_turn(tool_input: dict, call_id: str = "id_1") -> LLMTurn:
    return LLMTurn(
        stop_reason="tool_use",
        text="",
        tool_calls=[ToolCall(id=call_id, name="get_issue_details", input=tool_input)],
        assistant_message={"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "get_issue_details", "input": tool_input}
        ]},
    )


def _issue_attachments_turn(tool_input: dict, call_id: str = "ia_1") -> LLMTurn:
    return LLMTurn(
        stop_reason="tool_use",
        text="",
        tool_calls=[ToolCall(id=call_id, name="get_issue_attachments", input=tool_input)],
        assistant_message={"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "get_issue_attachments", "input": tool_input}
        ]},
    )


def _tool_use_turn(query: str, call_id: str = "call_1") -> LLMTurn:
    return LLMTurn(
        stop_reason="tool_use",
        text="",
        tool_calls=[ToolCall(id=call_id, name="search_knowledge_base", input={"query": query})],
        assistant_message={"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "search_knowledge_base", "input": {"query": query}}
        ]},
    )


def _end_turn(text: str) -> LLMTurn:
    return LLMTurn(
        stop_reason="end_turn", text=text, tool_calls=[],
        assistant_message={"role": "assistant", "content": [{"type": "text", "text": text}]},
    )


class Hit:
    def __init__(self, id, issue_key="X-1", chunk_type="issue", source_id=1, content="c", score=1.0, retrievers=None, page_number=None):
        self.id, self.issue_key, self.chunk_type, self.source_id = id, issue_key, chunk_type, source_id
        self.content, self.score, self.retrievers = content, score, retrievers or ["vector"]
        self.page_number = page_number


def test_initial_router_matches_the_reported_regression_questions():
    assert _initial_tool_choice("how many sprints do we have so far") == "query_sprints"
    assert (
        _initial_tool_choice(
            "how many issues do we have so far? give me the result including sprint7 and without"
        )
        == "query_sprints"
    )
    assert _initial_tool_choice("which issues are currently blocked?") == "query_issues"
    assert _initial_tool_choice("what is blocking the payment system?") == "search_knowledge_base"


async def test_single_retrieval_then_answer():
    llm = FakeLLM([_tool_use_turn("payment errors"), _end_turn("root cause was X (X-1)")])
    retrieval = FakeRetrieval(default=[Hit("issue:1")])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("why did payment fail?", space_ids=[7])

    assert result.retrieval_rounds == 1
    assert result.queries_used == ["payment errors"]
    assert result.abstained is False
    assert len(result.citations) == 1


async def test_blank_end_turn_text_falls_back_to_abstention():
    # Regression test for a real bug found live (not hypothesized): qwen3.6-35b-a3b, via LM Studio,
    # returned stop_reason="end_turn" with empty text after retrieval found nothing relevant — not
    # the required ABSTENTION_PHRASE, just silence. An empty string is never a valid answer to hand
    # back to a caller.
    llm = FakeLLM([_tool_use_turn("obscure metric"), _end_turn("")])
    retrieval = FakeRetrieval(default=[])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what's our NPS score?", space_ids=[7])

    assert result.abstained is True
    assert result.text == ABSTENTION_PHRASE


async def test_corrective_retrieval_reformulates_and_retries():
    # First search returns nothing useful; model reformulates and searches again; then answers.
    llm = FakeLLM([
        _tool_use_turn("vague query", call_id="c1"),
        _tool_use_turn("HikariCP connection pool", call_id="c2"),
        _end_turn("fixed via pool tuning (X-1)"),
    ])
    retrieval = FakeRetrieval(results_by_query={
        "vague query": [],
        "HikariCP connection pool": [Hit("comment:9")],
    })
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("why did checkout fail?", space_ids=[7])

    assert result.retrieval_rounds == 2
    assert result.queries_used == ["vague query", "HikariCP connection pool"]
    assert len(result.citations) == 1


async def test_abstains_when_nothing_relevant_found():
    llm = FakeLLM([_tool_use_turn("obscure topic"), _end_turn(ABSTENTION_PHRASE)])
    retrieval = FakeRetrieval(default=[])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what is our policy on quantum encryption?", space_ids=[7])

    assert result.abstained is True
    assert result.text == ABSTENTION_PHRASE


async def test_max_iterations_caps_retrieval_and_forces_final_answer():
    # Model tries to search on every turn up to (and past) the cap. With max_iterations=2, the loop
    # makes 3 tool-use decision points (iterations 0, 1, 2), then forces a final answer call — and
    # that final call is made with an EMPTY tools list, so a real model literally cannot emit
    # another tool_use there (nothing to call). FakeLLM can't express "no tools offered" since it's
    # a dumb scripted queue, so the 4th scripted turn stands in for "the model's only option now".
    turns = [_tool_use_turn(f"query {i}", call_id=f"c{i}") for i in range(3)]
    turns.append(_end_turn("giving up gracefully"))
    llm = FakeLLM(turns)
    retrieval = FakeRetrieval(default=[])
    agent = CragAgent(llm, retrieval, max_iterations=2, top_k=5)

    result = await agent.ask("anything", space_ids=[7])

    assert result.retrieval_rounds == 2  # capped, not 3
    assert result.text == ABSTENTION_PHRASE
    assert result.abstained is True
    assert llm.calls == 4  # 3 tool-use decisions + 1 forced final (tools=[])


async def test_forced_stop_falls_back_safely_if_model_ignores_empty_tools():
    # Defensive path: if a provider somehow still returns tool_use (or blank text) on the
    # tools=[] forced-final call, the loop must not silently return blank/garbage text.
    turns = [_tool_use_turn(f"query {i}", call_id=f"c{i}") for i in range(3)]
    turns.append(_tool_use_turn("still trying", call_id="c99"))  # ignores the empty tools hint
    llm = FakeLLM(turns)
    retrieval = FakeRetrieval(default=[])
    agent = CragAgent(llm, retrieval, max_iterations=2, top_k=5)

    result = await agent.ask("anything", space_ids=[7])

    assert result.text == ABSTENTION_PHRASE
    assert result.abstained is True


async def test_space_ids_are_never_taken_from_the_model():
    # The model's tool_use input only ever contains `query`/`mode` (see _SEARCH_TOOL's schema) — the
    # loop must pass the caller's space_ids on every call, regardless of what a (hypothetically
    # malicious or confused) model tries to put in the tool arguments.
    llm = FakeLLM([_tool_use_turn("q"), _end_turn("done")])
    retrieval = FakeRetrieval(default=[])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("question", space_ids=[42, 43])

    assert retrieval.calls[0][1] == (42, 43)


async def test_history_is_prepended_before_the_new_question():
    llm = FakeLLM([
        _tool_use_turn("bug issue keys"),
        _end_turn(ABSTENTION_PHRASE),
    ])
    agent = CragAgent(llm, FakeRetrieval(), max_iterations=4, top_k=5)
    history = [
        {"role": "user", "content": "how many bugs do we have?"},
        {"role": "assistant", "content": "There are 11 bugs, all done."},
    ]

    await agent.ask("what are their issue keys?", space_ids=[7], history=history)

    first_call_messages = llm.messages_seen[0]
    assert first_call_messages[0] == history[0]
    assert first_call_messages[1] == history[1]
    assert first_call_messages[2] == {"role": "user", "content": "what are their issue keys?"}


async def test_no_history_behaves_exactly_as_a_fresh_conversation():
    llm = FakeLLM([
        _tool_use_turn("a question"),
        _end_turn(ABSTENTION_PHRASE),
    ])
    agent = CragAgent(llm, FakeRetrieval(), max_iterations=4, top_k=5)

    await agent.ask("a question", space_ids=[7])  # history omitted entirely

    assert llm.messages_seen[0] == [{"role": "user", "content": "a question"}]


async def test_query_issues_tool_is_dispatched_for_count_questions():
    # A "how many bugs" question routes to the structured tool, not semantic search — and the exact
    # count flows through to the answer.
    result_data = IssueQueryResult(
        total_count=11, counts_by_type={"bug": 11}, counts_by_status={"done": 11}, issues=[]
    )
    llm = FakeLLM([
        _issue_query_turn({"issue_types": ["bug"]}),
        _end_turn("There are 11 bug issues."),
    ])
    retrieval = FakeRetrieval(issue_result=result_data)
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("how many bugs do we have?", space_ids=[5000014])

    assert len(retrieval.issue_calls) == 1
    assert retrieval.calls == []  # semantic search was NOT used
    assert result.retrieval_rounds == 1
    assert "11" in result.text


async def test_query_issues_space_ids_injected_not_taken_from_model():
    # Same security boundary as semantic search: the loop injects the caller's space_ids into the
    # structured query, never the model's tool input.
    llm = FakeLLM([_issue_query_turn({"issue_types": ["story"]}), _end_turn("done")])
    retrieval = FakeRetrieval()
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("list the stories", space_ids=[42, 43])

    assert retrieval.issue_calls[0][0] == (42, 43)


def _history_turn(tool_input: dict, call_id: str = "h_1") -> LLMTurn:
    return LLMTurn(
        stop_reason="tool_use",
        text="",
        tool_calls=[ToolCall(id=call_id, name="get_issue_history", input=tool_input)],
        assistant_message={"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": "get_issue_history", "input": tool_input}
        ]},
    )


async def test_issue_history_tool_is_dispatched_for_reopen_questions():
    history = IssueHistoryResult(
        total_count=2,
        changes=[
            {"issue_key": "ATC-68", "event_type": "field_change", "field_name": "status",
             "from_value": "done", "to_value": "in_progress", "actor_name": "Daniel Park",
             "changed_at": "2026-07-02T16:00:00Z"},
            {"issue_key": "ATC-30", "event_type": "field_change", "field_name": "status",
             "from_value": "done", "to_value": "in_progress", "actor_name": "Noah Kim",
             "changed_at": "2026-05-14T17:20:00Z"},
        ],
    )
    llm = FakeLLM([
        _history_turn({"reopened_only": True}),
        _end_turn("2 issues were reopened: ATC-68 and ATC-30."),
    ])
    retrieval = FakeRetrieval(history_result=history)
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("which issues were reopened?", space_ids=[5000014])

    assert retrieval.history_calls == [((5000014,), {"reopened_only": True})]
    # No search_knowledge_base call; each registered verifier performs its own structured
    # issue-type/status lookup over the same mentioned keys (subtask-claim check, then
    # current-state check — the answer text says "reopened", which is a status hint).
    assert retrieval.calls == []
    assert retrieval.issue_calls == [
        ((5000014,), {"issue_keys": ["ATC-30", "ATC-68"]}),
        ((5000014,), {"issue_keys": ["ATC-30", "ATC-68"]}),
    ]
    assert "ATC-68" in result.text


async def test_issue_history_space_ids_injected_and_input_whitelisted():
    llm = FakeLLM([
        _history_turn({"issue_keys": ["ATC-77"], "space_ids": [999], "sql": "drop table"}),
        _end_turn("done"),
    ])
    retrieval = FakeRetrieval()
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("what did I change in ATC-77?", space_ids=[7])

    space_ids, filters = retrieval.history_calls[0]
    assert space_ids == (7,)
    assert filters == {"issue_keys": ["ATC-77"]}  # model-supplied space_ids/sql stripped


async def test_system_prompt_carries_current_time_for_relative_time_questions():
    # "what did I change 10 minutes ago" is unanswerable without a clock — the system prompt must
    # embed the ask-time UTC timestamp, not a stale module-load constant.
    captured = {}

    class CapturingLLM(FakeLLM):
        async def next_turn(self, system, messages, tools, tool_choice=None):
            captured["system"] = system
            captured["tools"] = [t["name"] for t in tools]
            return await super().next_turn(system, messages, tools, tool_choice=tool_choice)

    llm = CapturingLLM([
        _tool_use_turn("anything"),
        _end_turn(ABSTENTION_PHRASE),
    ])
    agent = CragAgent(llm, FakeRetrieval(), max_iterations=4, top_k=5)
    await agent.ask("anything", space_ids=[7])

    import re
    from datetime import datetime, timezone
    m = re.search(r"current UTC time is (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", captured["system"])
    assert m, "system prompt is missing the current-time stamp"
    stamped = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    # Fresh (within one 5-minute rounding window of now) — built at ask() time, not a stale constant.
    assert 0 <= (datetime.now(timezone.utc) - stamped).total_seconds() < 300
    # Rounded to a 5-minute mark, not second-precision — a prompt-caching requirement (see
    # _current_time_for_prompt's docstring): the stamp must repeat across calls within the same
    # window, or the "identical prefix" a cache hit needs never occurs.
    assert stamped.second == 0 and stamped.minute % 5 == 0
    assert captured["tools"] == [
        "search_knowledge_base", "query_issues", "query_sprints", "get_issue_history",
        "get_issue_comments", "get_issue_details", "get_issue_attachments",
    ]


async def test_concurrent_asks_keep_their_own_system_prompt(monkeypatch):
    """A shared agent must not swap request A's clock for request B's between LLM turns."""
    timestamps = iter(["2030-01-01T00:00:00Z", "2040-02-02T00:00:00Z"])
    monkeypatch.setattr(crag_loop, "_current_time_for_prompt", lambda: next(timestamps))
    second_request_started = asyncio.Event()

    class InterleavingLLM:
        def __init__(self):
            self.calls_by_question = {}
            self.systems_by_question = {}

        async def next_turn(self, system, messages, tools, tool_choice=None):
            question = messages[0]["content"]
            call_number = self.calls_by_question.get(question, 0) + 1
            self.calls_by_question[question] = call_number
            self.systems_by_question.setdefault(question, []).append(system)
            if question == "request A" and call_number == 1:
                await second_request_started.wait()
            elif question == "request B" and call_number == 1:
                second_request_started.set()
            return _tool_use_turn(question, f"{question}-{call_number}") if call_number == 1 else _end_turn("done")

    llm = InterleavingLLM()
    agent = CragAgent(llm, FakeRetrieval(), max_iterations=4, top_k=5)

    first = asyncio.create_task(agent.ask("request A", space_ids=[7]))
    await asyncio.sleep(0)
    second = asyncio.create_task(agent.ask("request B", space_ids=[7]))
    await asyncio.gather(first, second)

    assert all("2030-01-01T00:00:00Z" in prompt for prompt in llm.systems_by_question["request A"])
    assert all("2040-02-02T00:00:00Z" in prompt for prompt in llm.systems_by_question["request B"])


async def test_query_issues_strips_non_whitelisted_fields_from_model_input():
    # A model that tries to smuggle space_ids (or any unknown key) into the structured tool input must
    # not have it forwarded downstream — only whitelisted filter fields pass through.
    llm = FakeLLM([
        _issue_query_turn({"issue_types": ["bug"], "space_ids": [999], "evil": "x"}),
        _end_turn("done"),
    ])
    retrieval = FakeRetrieval()
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("how many bugs", space_ids=[7])

    space_ids, filters = retrieval.issue_calls[0]
    assert space_ids == (7,)  # caller's, not the model's 999
    assert filters == {"issue_types": ["bug"]}  # space_ids/evil stripped


async def test_query_issues_passes_sprint_filters_through():
    # sprint_ids/sprint_names must actually reach the retrieval client — a regression here would be
    # the tool schema advertising a filter the whitelist silently drops.
    llm = FakeLLM([
        _sprint_query_turn({}),
        _issue_query_turn({"sprint_ids": [501], "issue_types": ["bug"]}),
        _end_turn("done"),
    ])
    retrieval = FakeRetrieval()
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("how many bugs in sprint 7", space_ids=[7])

    assert retrieval.sprint_calls == [((7,), {})]
    assert retrieval.issue_calls[0][1] == {"sprint_ids": [501], "issue_types": ["bug"]}


async def test_query_issues_passes_priorities_filter_through():
    # Regression test: priorities was advertised in the tool schema and documented in the system
    # prompt, but the whitelist in _run_issue_query omitted it, so it was silently dropped before
    # reaching the retrieval client — every priority-filtered question silently fell back to an
    # unfiltered query (see docs/RAG_ACCURACY_CASE_STUDIES.md).
    llm = FakeLLM([
        _issue_query_turn({"priorities": ["highest"]}),
        _end_turn("done"),
    ])
    retrieval = FakeRetrieval()
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("how many issues are highest priority", space_ids=[7])

    assert retrieval.issue_calls[0][1] == {"priorities": ["highest"]}


async def test_query_issues_passes_issue_keys_for_topic_current_state_intersection():
    llm = FakeLLM([
        _issue_query_turn({"issue_keys": ["ATC-34", "ATC-55"], "statuses": ["blocked"]}),
        _end_turn("ATC-34 is currently blocked."),
    ])
    retrieval = FakeRetrieval()
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("which issues are blocked?", space_ids=[7])

    assert retrieval.issue_calls[0][1] == {
        "issue_keys": ["ATC-34", "ATC-55"],
        "statuses": ["blocked"],
    }


async def test_obvious_sprint_count_forces_structured_tool_first():
    llm = FakeLLM([_sprint_query_turn({}), _end_turn("There are exactly 7 sprints.")])
    retrieval = FakeRetrieval(
        sprint_result=SprintQueryResult(
            total_count=7, counts_by_status={"completed": 6, "active": 1}, sprints=[]
        )
    )
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("how many sprints do we have so far?", space_ids=[7])

    assert llm.tool_choices_seen[0] == "query_sprints"
    assert retrieval.sprint_calls == [((7,), {})]
    assert "7" in result.text


async def test_unsupported_issue_key_in_answer_is_rejected_and_sources_are_hidden():
    llm = FakeLLM([
        _tool_use_turn("payment blocker"),
        _end_turn("The payment system is blocked by ATC-77."),
    ])
    retrieval = FakeRetrieval(default=[
        Hit("issue:34", issue_key="ATC-34", content="Checkout beta is blocked."),
        Hit("issue:55", issue_key="ATC-55", content="Sign-in was unblocked."),
    ])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what is blocking the payment system?", space_ids=[7])

    assert result.text == ABSTENTION_PHRASE
    assert result.abstained is True
    assert result.citations == []


async def test_sources_only_include_issue_keys_actually_cited_by_answer():
    llm = FakeLLM([
        _tool_use_turn("payment blocker"),
        _end_turn("Checkout is blocked by request-ID work (ATC-34)."),
    ])
    retrieval = FakeRetrieval(default=[
        Hit("issue:34", issue_key="ATC-34", content="Checkout beta is blocked."),
        Hit("issue:55", issue_key="ATC-55", content="Sign-in was unblocked."),
        Hit("comment:34", issue_key="ATC-34", content="Duplicate chunk for the same issue."),
    ])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what is blocking the payment system?", space_ids=[7])

    assert result.abstained is False
    assert [citation.issue_key for citation in result.citations] == ["ATC-34"]


async def test_order_id_in_answer_is_not_mistaken_for_an_unsupported_issue_key():
    # Regression: the corpus's own text contains non-issue-key identifiers of the same PREFIX-NNN
    # shape — e.g. order references like "BETA-1042" quoted inside a comment. When the model
    # faithfully quotes them from a retrieved chunk, the hallucination guard must NOT treat them as
    # unsupported issue references and nuke the whole (correct, grounded) answer. Found live: every
    # question about the double-order incident abstained for exactly this reason. Only tokens sharing
    # a project prefix with retrieved evidence (here ATC-) are held to the "must be in evidence" rule.
    llm = FakeLLM([
        _tool_use_turn("double order checkout"),
        _end_turn("Clicking Place order twice created two orders, BETA-1042 and BETA-1043, and "
                  "dropped stock by two (ATC-43)."),
    ])
    retrieval = FakeRetrieval(default=[
        Hit("comment:43", issue_key="ATC-43",
            content="Two order rows show up: BETA-1042 and BETA-1043. Stock drops by two."),
    ])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("how was the double-order checkout bug reproduced?", space_ids=[7])

    assert result.abstained is False
    assert "BETA-1042" in result.text
    assert [citation.issue_key for citation in result.citations] == ["ATC-43"]


async def test_hallucinated_key_sharing_a_real_project_prefix_is_still_rejected():
    # The prefix-aware relaxation above must NOT weaken the guard for genuine hallucinations: a made-up
    # ATC-9999 shares the ATC- prefix that IS in evidence, so it's still an unsupported issue key.
    llm = FakeLLM([
        _tool_use_turn("payment blocker"),
        _end_turn("Checkout is blocked by ATC-9999."),
    ])
    retrieval = FakeRetrieval(default=[Hit("issue:34", issue_key="ATC-34", content="Checkout beta.")])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what is blocking the payment system?", space_ids=[7])

    assert result.text == ABSTENTION_PHRASE
    assert result.abstained is True


async def test_issue_key_mentioned_only_inside_a_retrieved_chunks_own_text_is_not_hallucination():
    # Regression: a retrieved chunk's own prose can legitimately reference OTHER issue keys as
    # backstory (e.g. ATC-43's comment says "split into ATC-44, ATC-45..."). Those keys were never
    # independently retrieved this turn, so without scanning retrieved content for them, the model
    # faithfully quoting that backstory got rejected as "unsupported issue references." Found live:
    # "show me the details of ATC-43" abstained because its own answer named the follow-up issues its
    # own retrieved comment mentioned.
    llm = FakeLLM([
        _tool_use_turn("ATC-43"),
        _end_turn("ATC-43 was split into follow-ups ATC-44 (idempotency fix) and ATC-45 (button "
                  "mitigation)."),
    ])
    retrieval = FakeRetrieval(
        default=[
            Hit("comment:43", issue_key="ATC-43",
                content="Split the follow-up into ATC-44 for the idempotency fix and ATC-45 for the "
                        "button mitigation."),
        ]
    )
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("show me the details of ATC-43", space_ids=[7])

    assert result.abstained is False
    assert "ATC-44" in result.text and "ATC-45" in result.text


async def test_sprint_name_citation_via_semantic_search_alone_is_not_forced_to_abstain():
    # Regression: a sprint chunk's provenance label is its sprint NAME (e.g. "AtlasCart Sprint 7"),
    # not an issue key — it never matches the PREFIX-NNN issue-key shape, so a semantic-only answer
    # that legitimately cites a sprint goal could never satisfy "must reference something retrieved"
    # and always got rejected. Found live: a follow-up ("does it have a goal?") that answered purely
    # from a semantic sprint-goal hit (no structured tool call that turn) abstained despite being
    # correct and fully grounded.
    llm = FakeLLM([
        _tool_use_turn("AtlasCart Sprint 7 goal"),
        _end_turn('Yes — AtlasCart Sprint 7 has the goal: "Prove the storefront is accessible."'),
    ])
    retrieval = FakeRetrieval(default=[
        Hit("sprint:7", issue_key="AtlasCart Sprint 7", chunk_type="sprint",
            content="Prove the storefront is accessible."),
    ])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what was focused on accessibility?", space_ids=[7])

    assert result.abstained is False
    assert [c.issue_key for c in result.citations] == ["AtlasCart Sprint 7"]


async def test_get_issue_comments_is_dispatched_and_grounds_the_answer_with_citations():
    # Plan-B fallback: search_knowledge_base's top-K comes back empty/weak, so the model falls back
    # to get_issue_comments on the issue it already knows the key for, and the returned comment text
    # is what actually grounds the answer. Phrased to avoid _initial_tool_choice's own routing (no
    # "reopen"/count/sprint trigger words) so the FIRST forced tool is search_knowledge_base, matching
    # how this fallback is actually meant to fire — only after a real search attempt first.
    comments_result = IssueCommentsResult(
        total_count=1,
        comments=[{
            "issue_id": 30, "issue_key": "ATC-30", "source_id": 501,
            "content": "Reopening this because the beta incident shows one browser action can "
                       "submit twice.",
        }],
    )
    llm = FakeLLM([
        _tool_use_turn("ATC-30 extra work needed"),
        _issue_comments_turn({"issue_keys": ["ATC-30"]}),
        _end_turn("ATC-30 needed extra work because the beta incident showed one browser action "
                  "could submit the checkout twice (ATC-30)."),
    ])
    retrieval = FakeRetrieval(default=[], comments_result=comments_result)
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("why did ATC-30 need extra work?", space_ids=[7])

    assert retrieval.comments_calls == [((7,), ("ATC-30",))]
    assert result.abstained is False
    assert [c.issue_key for c in result.citations] == ["ATC-30"]


async def test_get_issue_details_is_dispatched_and_grounds_the_answer_with_citations():
    # Reproduces the live bug (docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 11): search_knowledge_base
    # returns only COMMENT hits for an issue with many comments (its own title+description chunk
    # ranked below them), so the model must fall back to get_issue_details on the key it already
    # knows to actually retrieve the issue's own body — a pile of comment hits is not proof the
    # issue's own description came back.
    details_result = IssueDetailsResult(
        total_count=1,
        details=[{
            "issue_id": 5000932, "issue_key": "ATC-43", "source_id": 5000932,
            "content": "Checkout can create two orders from a double click. During the private beta, "
                       "a slow checkout allowed two Place order clicks.",
        }],
    )
    llm = FakeLLM([
        _tool_use_turn("ATC-43 details"),
        _issue_details_turn({"issue_keys": ["ATC-43"]}),
        _end_turn("ATC-43 is about checkout creating two orders from a double click: during the "
                  "private beta, a slow checkout allowed two Place order clicks (ATC-43)."),
    ])
    retrieval = FakeRetrieval(
        default=[Hit("comment:5003388", issue_key="ATC-43", chunk_type="comment",
                     content="Split the follow-up into ATC-44, ATC-45, and ATC-47.")],
        details_result=details_result,
    )
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("show me the details of ATC-43", space_ids=[7])

    assert retrieval.details_calls == [((7,), ("ATC-43",))]
    assert result.abstained is False
    assert [c.issue_key for c in result.citations] == ["ATC-43"]
    assert "double click" in result.citations[0].content


async def test_get_issue_attachments_is_dispatched_and_grounds_the_answer_with_citations():
    # Reproduces the live flakiness (docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 24):
    # search_knowledge_base's semantic/hybrid ranking can only be as good as the words the model
    # guesses, so an exact fact it doesn't already know the wording of (a SKU code) can genuinely miss
    # even though it's in the index — found live, this made "what SKU does ATC-46's attachment use"
    # flip between finding the answer and abstaining across equivalent phrasings. Once the issue key
    # is known, get_issue_attachments makes the lookup exact instead of a ranking gamble.
    attachments_result = IssueAttachmentsResult(
        total_count=1,
        attachments=[{
            "issue_id": 5000935, "issue_key": "ATC-46", "source_id": 5000940,
            "page_number": 2,
            "provenance": {"source_type": "pdf", "page_number": 2},
            "content": "Acceptance criteria table: SKU A-104, order reference BETA-1043, quantity 1.",
        }],
    )
    llm = FakeLLM([
        _tool_use_turn("ATC-46 attachment"),
        _issue_attachments_turn({"issue_keys": ["ATC-46"]}),
        _end_turn("The worked example uses SKU A-104 and order reference BETA-1043 (ATC-46)."),
    ])
    retrieval = FakeRetrieval(
        default=[],
        attachments_result=attachments_result,
    )
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what SKU does ATC-46's attachment use?", space_ids=[7])

    assert retrieval.attachments_calls == [((7,), ("ATC-46",))]
    assert result.abstained is False
    assert [c.issue_key for c in result.citations] == ["ATC-46"]
    assert "A-104" in result.citations[0].content
    assert result.citations[0].page_number == 2
    assert result.citations[0].provenance["source_type"] == "pdf"


async def test_citation_picks_the_chunk_that_actually_supports_the_answer_not_the_first_one():
    # Regression, found live: get_issue_comments can return MANY comments for one issue, all tied at
    # score=0.0 (it's deliberately unranked — see its docstring). The old citation selection took the
    # FIRST chunk seen for a cited issue key, which was a safe shortcut when every citation came from
    # search_knowledge_base's already-relevance-ranked top-K, but breaks badly here: a question about
    # why ATC-30 was reopened got a correct answer grounded in comment #4 of 6, while the UI's
    # "Sources" showed comment #1 — an unrelated story-point-estimate note that merely happened to be
    # chronologically first. Selection must prefer the chunk whose content actually overlaps with
    # what the answer says, not an arbitrary same-issue chunk.
    comments_result = IssueCommentsResult(
        total_count=2,
        comments=[
            {"issue_id": 30, "issue_key": "ATC-30", "source_id": 1,
             "content": "The first estimate missed price snapshots and safe retries. Moving this "
                        "to eight before implementation starts."},
            {"issue_id": 30, "issue_key": "ATC-30", "source_id": 2,
             "content": "Reopening this because a beta incident showed one browser action could "
                        "submit twice. Not complete until the API handles repeated requests safely."},
        ],
    )
    llm = FakeLLM([
        _tool_use_turn("ATC-30 extra work reason"),
        _issue_comments_turn({"issue_keys": ["ATC-30"]}),
        _end_turn("ATC-30 needed the extra work because a beta incident showed one browser action "
                  "could submit twice, and the API needed to handle repeated requests safely (ATC-30)."),
    ])
    retrieval = FakeRetrieval(default=[], comments_result=comments_result)
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("why did ATC-30 need extra work?", space_ids=[7])

    assert result.abstained is False
    assert len(result.citations) == 1
    assert "submit twice" in result.citations[0].content
    assert "price snapshots" not in result.citations[0].content


async def test_field_change_description_is_included_when_it_carries_the_actual_reason():
    # Regression: _format_issue_history used to drop `description` entirely for field_change events,
    # even when it held the real narrative (e.g. a reopen reason an actor typed in) rather than a
    # generic "Updated status" — silently hiding the answer to "why" from the model even though it
    # was right there in the structured tool result it already had.
    result = IssueHistoryResult(
        total_count=1,
        changes=[{
            "issue_key": "ATC-68", "event_type": "field_change", "field_name": "status",
            "from_value": "done", "to_value": "in_progress",
            "description": "Daniel reopened the issue after the resent-email check failed",
            "actor_name": "Daniel Park", "changed_at": "2026-07-02T16:00:00Z",
        }],
    )

    text = _format_issue_history(result, {})

    assert "resent-email check failed" in text

    # A generic, contentless description shouldn't clutter the line with redundant text.
    generic = IssueHistoryResult(
        total_count=1,
        changes=[{
            "issue_key": "ATC-30", "event_type": "field_change", "field_name": "status",
            "from_value": "in_progress", "to_value": "done", "description": "Updated status",
            "actor_name": "Noah Kim", "changed_at": "2026-05-13T21:50:00Z",
        }],
    )
    generic_text = _format_issue_history(generic, {})
    assert generic_text.count("Updated status") == 0


async def test_query_sprints_tool_is_dispatched_and_reports_exact_count():
    result_data = SprintQueryResult(
        total_count=7,
        counts_by_status={"completed": 6, "active": 1},
        sprints=[{"sprint_id": 501, "sprint_name": "Sprint 7", "status": "active",
                  "goal": "Prove the storefront is accessible.", "start_date": "2026-07-13",
                  "end_date": "2026-07-24", "completed_points": 12, "final_scope_points": 20,
                  "initial_committed_points": 18}],
    )
    llm = FakeLLM([_sprint_query_turn({"statuses": ["active"]}), _end_turn("Sprint 7 is active.")])
    retrieval = FakeRetrieval(sprint_result=result_data)
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("which sprint is active?", space_ids=[5000014])

    assert len(retrieval.sprint_calls) == 1
    assert retrieval.calls == [] and retrieval.issue_calls == []  # only the sprint tool fired
    assert "Sprint 7" in result.text


async def test_query_sprints_space_ids_injected_and_input_whitelisted():
    llm = FakeLLM([
        _sprint_query_turn({"statuses": ["active"], "space_ids": [999], "evil": "x"}),
        _end_turn("done"),
    ])
    retrieval = FakeRetrieval()
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    await agent.ask("which sprint is active?", space_ids=[42, 43])

    space_ids, filters = retrieval.sprint_calls[0]
    assert space_ids == (42, 43)  # caller's, not the model's 999
    assert filters == {"statuses": ["active"]}  # space_ids/evil stripped


async def test_verification_still_fires_on_the_forced_stop_path():
    # Regression for eval Finding 2: previously the correction round was implemented by looping
    # back into the SAME iteration counter that bounds the model's own tool-calling, gated by
    # `iteration < max_iterations + 1` — a reserved slot that the model's own research could consume
    # first. With max_iterations=2, a model that keeps calling tools right up to the cap forces the
    # final answer through the tools=[] forced-stop branch, which used to return immediately without
    # ever running the post-generation verifiers. Here the forced answer wrongly presents ATC-46 (a
    # different bug) as an equivalent subtask alongside the real subtask ATC-44 — this must still get
    # caught and corrected even though it came from the forced-stop branch, not a natural end_turn.
    mismatch_text = "ATC-43's follow-ups are ATC-44 and ATC-46."
    corrected_text = "ATC-43's real subtask is ATC-44; ATC-46 is a separate, related bug."
    turns = [_tool_use_turn(f"query {i}", call_id=f"c{i}") for i in range(3)]
    turns.append(_end_turn(mismatch_text))  # the forced final call (tools=[])
    turns.append(_end_turn("EQUIVALENT"))  # _check_subtask_claims' classification call
    turns.append(_end_turn(corrected_text))  # the correction round's own forced call (tools=[])
    llm = FakeLLM(turns)
    issue_result = IssueQueryResult(
        total_count=2, counts_by_type={}, counts_by_status={},
        issues=[
            {"issue_key": "ATC-44", "issue_type": "subtask", "parent_key": "ATC-43"},
            {"issue_key": "ATC-46", "issue_type": "bug", "parent_key": "ATC-4"},
        ],
    )
    retrieval = FakeRetrieval(
        default=[
            Hit("h0", issue_key="ATC-43"), Hit("h1", issue_key="ATC-44"), Hit("h2", issue_key="ATC-46"),
        ],
        issue_result=issue_result,
    )
    agent = CragAgent(llm, retrieval, max_iterations=2, top_k=5)

    result = await agent.ask("Summarize the follow-ups to ATC-43.", space_ids=[7])

    assert result.text == corrected_text
    assert result.abstained is False
    # Regression: found live in the v2 eval — the forced-stop branch called the correction round
    # without ever appending the forced final answer (`final.assistant_message`) to `messages`, so
    # the model doing the correction had no assistant turn in its own history to actually revise;
    # asking it to "revise your answer" against a conversation where it never said that answer is
    # not a real correction request. The LAST call the FakeLLM saw (the correction call) must include
    # the forced-stop text as a real assistant message.
    last_messages_seen = llm.messages_seen[-1]
    assert any(
        m.get("role") == "assistant" and m.get("content") == [{"type": "text", "text": mismatch_text}]
        for m in last_messages_seen
    )


async def test_query_issues_reports_parent_key_for_epic_lookup_questions():
    # Finding 4: the parent_key column was already threaded end-to-end into query_issues' response,
    # but nothing told the model it could use it to answer "which epic is X in" — it had no
    # deterministic way to answer that question at all. The formatted tool result must surface it.
    result_data = IssueQueryResult(
        total_count=1, counts_by_type={}, counts_by_status={},
        issues=[{"issue_key": "ATC-77", "issue_type": "story", "status": "blocked",
                 "sprint_name": "Sprint 7", "parent_key": "ATC-52", "updated_at": "2026-07-27",
                 "title": "Cache the public product catalog safely"}],
    )
    llm = FakeLLM([_issue_query_turn({"issue_keys": ["ATC-77"]}), _end_turn("ATC-77's epic is ATC-52.")])
    retrieval = FakeRetrieval(issue_result=result_data)
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("which epic does ATC-77 belong to?", space_ids=[5000014])

    assert "ATC-52" in result.text


async def test_correction_message_instructs_the_model_not_to_narrate_the_fix():
    # Finding 7: a live run showed the corrected answer leaking "You're right, let me revise..." to
    # the end user, who never saw the flagged/original answer and has no idea a correction happened.
    from app.agent.crag_loop import _verification_correction_message, ClaimMismatch

    message = _verification_correction_message([ClaimMismatch(issue_key="ATC-46", detail="some detail")])

    content = message["content"].lower()
    assert "do not mention this correction" in content
    assert "never saw" in content


async def test_current_state_verifier_corrects_a_stale_status_claim():
    # Finding 1: deep multi-hop synthesis can let an earlier comment's state win out over a later,
    # authoritative status change (ground rule 4 says to prefer current state) — this is the same
    # registry/one-shot-correction architecture already proven for subtask claims (Case Study 15/17),
    # extended to a second claim type instead of adding a new bespoke mechanism.
    stale_text = "ATC-43 is still open and unresolved."
    corrected_text = "ATC-43 is done."
    llm = FakeLLM([
        _tool_use_turn("checkout bug status"),
        _end_turn(stale_text),
        _end_turn('["ATC-43"]'),  # _check_current_state_claims' classification call
        _end_turn(corrected_text),  # the correction round's forced call (tools=[])
    ])
    issue_result = IssueQueryResult(
        total_count=1, counts_by_type={}, counts_by_status={},
        issues=[{"issue_key": "ATC-43", "issue_type": "bug", "status": "done"}],
    )
    retrieval = FakeRetrieval(default=[Hit("issue:43", issue_key="ATC-43")], issue_result=issue_result)
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what is the status of ATC-43?", space_ids=[7])

    assert result.text == corrected_text


async def test_current_state_verifier_does_not_fire_without_a_status_word():
    # The regex is only a cost-gating precondition, not a whack-a-mole detector — an answer with no
    # status-like vocabulary at all should never trigger the extra query_issues call.
    llm = FakeLLM([_tool_use_turn("checkout bug details"), _end_turn("ATC-43 is about a checkout bug.")])
    retrieval = FakeRetrieval(default=[Hit("issue:43", issue_key="ATC-43")])
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)

    result = await agent.ask("what is ATC-43 about?", space_ids=[7])

    assert retrieval.issue_calls == []
    assert result.text == "ATC-43 is about a checkout bug."
