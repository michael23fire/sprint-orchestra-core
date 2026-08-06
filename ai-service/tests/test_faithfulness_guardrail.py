"""Runtime faithfulness guardrail (app/agent/crag_loop.py's `_check_faithfulness`) — the online
counterpart to RAGAS's faithfulness metric (eval/ragas_eval.py), which only ever measures this
offline, after the fact, against a fixed golden set. Off by default (config.py's
`faithfulness_check_enabled`) — a real extra LLM call on every non-abstaining answer.
"""
from app.agent.crag_loop import CragAgent, RetrievedChunk
from app.llm.types import Usage

from .test_crag_loop import FakeLLM, FakeRetrieval, Hit, _end_turn, _tool_use_turn


def _citation(content: str, issue_key: str = "ATC-1") -> RetrievedChunk:
    return RetrievedChunk(
        id="chunk:1", chunk_type="issue", issue_id=1, issue_key=issue_key,
        source_id=1, content=content, score=1.0, retrievers=["vector"],
    )


async def test_check_faithfulness_skips_the_llm_call_when_context_is_empty():
    agent = CragAgent(FakeLLM([]), FakeRetrieval(), max_iterations=4, top_k=5)

    mismatches, usage = await agent._check_faithfulness("Some answer.", [], [])

    assert mismatches == []
    assert usage == Usage()


async def test_check_faithfulness_passes_when_llm_reports_supported():
    llm = FakeLLM([_end_turn("SUPPORTED")])
    agent = CragAgent(llm, FakeRetrieval(), max_iterations=4, top_k=5)

    mismatches, usage = await agent._check_faithfulness(
        "ATC-43 has 11 bugs.", [_citation("There are 11 bugs total.")], []
    )

    assert mismatches == []
    assert llm.calls == 1


async def test_check_faithfulness_flags_unsupported_claims():
    llm = FakeLLM([_end_turn("UNSUPPORTED: the fix shipped in version 2.3")])
    agent = CragAgent(llm, FakeRetrieval(), max_iterations=4, top_k=5)

    mismatches, usage = await agent._check_faithfulness(
        "The bug is fixed and shipped in version 2.3.",
        [_citation("The bug was fixed.")],
        [],
    )

    assert len(mismatches) == 1
    assert "version 2.3" in mismatches[0].detail


async def test_check_faithfulness_also_reads_structured_evidence_not_just_citations():
    llm = FakeLLM([_end_turn("SUPPORTED")])
    agent = CragAgent(llm, FakeRetrieval(), max_iterations=4, top_k=5)

    await agent._check_faithfulness(
        "There are 11 bugs.", [], ["Structured issue query: total_count = 11 (bug)"]
    )

    # The structured_evidence text must have reached the prompt, not just citations.
    sent_prompt = llm.messages_seen[0][0]["content"]
    assert "total_count = 11" in sent_prompt


async def test_disabled_by_default_never_calls_the_extra_faithfulness_check():
    llm = FakeLLM([_tool_use_turn("payment errors"), _end_turn("Root cause was X. (ATC-1)")])
    retrieval = FakeRetrieval(
        default=[Hit("issue:1", issue_key="ATC-1", content="Root cause was X.")]
    )
    agent = CragAgent(llm, retrieval, max_iterations=4, top_k=5)  # faithfulness_check_enabled defaults False

    await agent.ask("why did payments fail?", space_ids=[7])

    assert llm.calls == 2  # search turn + end_turn only, no extra faithfulness-check call


async def test_enabled_flag_triggers_correction_when_an_unsupported_claim_is_found():
    mismatch_answer = "The root cause was a connection pool leak, fixed by version 2.3. (ATC-1)"
    corrected_answer = "The root cause was a connection pool leak. (ATC-1)"
    llm = FakeLLM([
        _tool_use_turn("payment errors"),
        _end_turn(mismatch_answer),
        _end_turn("UNSUPPORTED: fixed by version 2.3"),  # the faithfulness check itself
        _end_turn(corrected_answer),  # the correction round's forced call
    ])
    retrieval = FakeRetrieval(
        default=[Hit("issue:1", issue_key="ATC-1", content="Root cause was a connection pool leak.")]
    )
    agent = CragAgent(
        llm, retrieval, max_iterations=4, top_k=5, faithfulness_check_enabled=True,
    )

    result = await agent.ask("why did payments fail?", space_ids=[7])

    assert result.text == corrected_answer
    assert llm.calls == 4
