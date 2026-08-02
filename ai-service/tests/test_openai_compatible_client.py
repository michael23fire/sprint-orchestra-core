"""OpenAI-compatible adapter regressions — no server or network required."""

from unittest.mock import AsyncMock

from app.llm.openai_compatible_client import _parse_text_tool_calls
from app.llm.openai_compatible_client import OpenAICompatibleClient


def test_recovers_xml_tool_call_instead_of_leaking_it_as_answer_text():
    text = """<tool_call>
<function=search_knowledge_base>
<parameter=query>
Sprint 1 Sprint 2 Sprint 3
</parameter>
<parameter=mode>
lexical
</parameter>
</function>
</tool_call>"""

    cleaned, calls = _parse_text_tool_calls(text)

    assert cleaned == ""
    assert len(calls) == 1
    assert calls[0].name == "search_knowledge_base"
    assert calls[0].input == {
        "query": "Sprint 1 Sprint 2 Sprint 3",
        "mode": "lexical",
    }


def test_recovers_json_tool_call_and_preserves_surrounding_text():
    text = (
        'I will check. <tool_call>{"name":"query_sprints","arguments":{}}</tool_call> '
        "Please wait."
    )

    cleaned, calls = _parse_text_tool_calls(text)

    assert cleaned == "I will check.  Please wait."
    assert calls[0].name == "query_sprints"
    assert calls[0].input == {}


def test_malformed_tool_markup_is_not_silently_removed():
    text = "<tool_call><function=query_sprints>"

    cleaned, calls = _parse_text_tool_calls(text)

    assert cleaned == text
    assert calls == []


async def test_named_tool_choice_is_forwarded_to_openai_compatible_server():
    class FakeCompletion:
        def model_dump(self):
            return {
                "choices": [{"message": {"content": "done", "tool_calls": []}}],
                "usage": {},
            }

    client = OpenAICompatibleClient(
        base_url="http://local.invalid/v1",
        api_key="test",
        model="local-model",
        max_output_tokens=100,
    )
    client._client.chat.completions.create = AsyncMock(return_value=FakeCompletion())

    await client.next_turn(
        "system",
        [{"role": "user", "content": "how many sprints?"}],
        [{"name": "query_sprints", "description": "query", "input_schema": {"type": "object"}}],
        tool_choice="query_sprints",
    )

    payload = client._client.chat.completions.create.call_args.kwargs
    assert payload["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in payload["tools"]] == ["query_sprints"]
    await client.aclose()
