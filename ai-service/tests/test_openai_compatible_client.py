"""OpenAI-compatible adapter regressions — no server or network required."""

from unittest.mock import AsyncMock

from app.llm.openai_compatible_client import _parse_text_tool_calls
from app.llm.openai_compatible_client import OpenAICompatibleClient
from app.llm.types import ToolCall


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
    class FakeResponse:
        def model_dump(self):
            return {"output": [], "usage": {}}

    client = OpenAICompatibleClient(
        base_url="http://local.invalid/v1",
        api_key="test",
        model="local-model",
        max_output_tokens=100,
    )
    client._client.responses.create = AsyncMock(return_value=FakeResponse())

    await client.next_turn(
        "system",
        [{"role": "user", "content": "how many sprints?"}],
        [{"name": "query_sprints", "description": "query", "input_schema": {"type": "object"}}],
        tool_choice="query_sprints",
    )

    payload = client._client.responses.create.call_args.kwargs
    assert payload["tool_choice"] == "required"
    assert [tool["name"] for tool in payload["tools"]] == ["query_sprints"]
    await client.aclose()


async def test_function_call_output_parsed_from_responses_api_output_array():
    class FakeResponse:
        def model_dump(self):
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "query_sprints",
                        "arguments": '{"sprint_ids": [7]}',
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    client = OpenAICompatibleClient(
        base_url="http://local.invalid/v1", api_key="test", model="local-model", max_output_tokens=100,
    )
    client._client.responses.create = AsyncMock(return_value=FakeResponse())

    turn = await client.next_turn(
        "system", [{"role": "user", "content": "which sprint?"}],
        [{"name": "query_sprints", "description": "query", "input_schema": {"type": "object"}}],
    )

    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0] == ToolCall(id="call_1", name="query_sprints", input={"sprint_ids": [7]})
    assert turn.usage.input_tokens == 10
    assert turn.usage.output_tokens == 5
    await client.aclose()


async def test_message_text_parsed_from_responses_api_output_array_not_sdk_output_text_property():
    # Regression test for a real bug caught live: `response.output_text` is a client-side SDK
    # *property* (openai.types.responses.Response.output_text) that aggregates message text — it does
    # NOT survive `.model_dump()`, which only serializes actual fields. A first version of this parser
    # read `body.get("output_text")` and silently got "" back on every real multi-turn call that had
    # a genuine text answer. Text must be read from the `output` array's `message` items instead.
    class FakeResponse:
        def model_dump(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "There are 3 sprints."}],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    client = OpenAICompatibleClient(
        base_url="http://local.invalid/v1", api_key="test", model="local-model", max_output_tokens=100,
    )
    client._client.responses.create = AsyncMock(return_value=FakeResponse())

    turn = await client.next_turn(
        "system", [{"role": "user", "content": "how many sprints?"}], [],
    )

    assert turn.stop_reason == "end_turn"
    assert turn.text == "There are 3 sprints."
    await client.aclose()


async def test_assistant_tool_use_and_tool_result_translate_to_responses_input_items():
    client = OpenAICompatibleClient(
        base_url="http://local.invalid/v1", api_key="test", model="local-model", max_output_tokens=100,
    )
    fake = AsyncMock(return_value=type("R", (), {"model_dump": lambda self: {"output": [], "usage": {}}})())
    client._client.responses.create = fake

    await client.next_turn(
        "system",
        [
            {"role": "user", "content": "how many sprints?"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "query_sprints", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "3 sprints"}],
            },
        ],
        [],
    )

    input_items = fake.call_args.kwargs["input"]
    assert {"role": "user", "content": "how many sprints?"} in input_items
    assert {"type": "function_call", "call_id": "call_1", "name": "query_sprints", "arguments": "{}"} in input_items
    assert {"type": "function_call_output", "call_id": "call_1", "output": "3 sprints"} in input_items
    # System prompt goes in "instructions", never inside the input item list.
    assert all(item.get("role") != "system" for item in input_items)
    assert fake.call_args.kwargs["instructions"] == "system"
    await client.aclose()
