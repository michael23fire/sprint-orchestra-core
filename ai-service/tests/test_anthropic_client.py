"""AnthropicClient prompt-caching tests — mocked SDK, no real API key/network.

Verifies the request shape (cache_control placement) and that cache usage counters are parsed from
the response, without needing ANTHROPIC_API_KEY (none is configured in this environment).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.llm.anthropic_client import AnthropicClient, _with_trailing_cache_breakpoint


def _fake_response(text="ok", cache_creation=0, cache_read=0, input_tokens=100, output_tokens=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


def _client() -> AnthropicClient:
    return AnthropicClient(api_key="test-key", model="claude-opus-4-8", max_output_tokens=2048)


async def test_system_prompt_gets_a_cache_breakpoint():
    client = _client()
    with patch.object(client._client.messages, "create", new=AsyncMock(return_value=_fake_response())) as mock_create:
        await client.next_turn("you are an assistant", [{"role": "user", "content": "hi"}], [])

    sent_system = mock_create.call_args.kwargs["system"]
    assert sent_system == [
        {"type": "text", "text": "you are an assistant", "cache_control": {"type": "ephemeral"}}
    ]


async def test_last_message_gets_a_trailing_cache_breakpoint():
    client = _client()
    messages = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "turn 2"},
    ]
    with patch.object(client._client.messages, "create", new=AsyncMock(return_value=_fake_response())) as mock_create:
        await client.next_turn("system", messages, [])

    sent_messages = mock_create.call_args.kwargs["messages"]
    assert sent_messages[0] == messages[0]  # earlier messages untouched
    assert sent_messages[1] == messages[1]
    assert sent_messages[2] == {
        "role": "user",
        "content": [{"type": "text", "text": "turn 2", "cache_control": {"type": "ephemeral"}}],
    }


async def test_cache_breakpoint_on_list_content_marks_only_the_last_block():
    # Tool-result turns already have list content (see crag_loop._format_hits usage) — the breakpoint
    # must land on the last block, not replace or reorder the others.
    messages = [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "result A"},
            {"type": "tool_result", "tool_use_id": "b", "content": "result B"},
        ],
    }]
    result = _with_trailing_cache_breakpoint(messages)

    assert result[0]["content"][0] == {"type": "tool_result", "tool_use_id": "a", "content": "result A"}
    assert result[0]["content"][1] == {
        "type": "tool_result", "tool_use_id": "b", "content": "result B",
        "cache_control": {"type": "ephemeral"},
    }


async def test_original_messages_list_is_never_mutated():
    # The agent loop reuses `messages` for every remaining round of the SAME ask() call — a stray
    # cache_control marker baked into the caller's own list would leak into later, unrelated turns.
    original = [{"role": "user", "content": "hello"}]
    _with_trailing_cache_breakpoint(original)
    assert original == [{"role": "user", "content": "hello"}]  # untouched


async def test_cache_usage_counters_are_parsed_from_response():
    client = _client()
    response = _fake_response(cache_creation=500, cache_read=1200, input_tokens=50, output_tokens=20)
    with patch.object(client._client.messages, "create", new=AsyncMock(return_value=response)):
        turn = await client.next_turn("system", [{"role": "user", "content": "hi"}], [])

    assert turn.usage.input_tokens == 50
    assert turn.usage.output_tokens == 20
    assert turn.usage.cache_creation_input_tokens == 500
    assert turn.usage.cache_read_input_tokens == 1200


async def test_missing_cache_fields_default_to_zero_not_a_crash():
    # Defensive path for an SDK/API response shape that predates cache usage reporting.
    client = _client()
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),  # no cache_* attrs at all
    )
    with patch.object(client._client.messages, "create", new=AsyncMock(return_value=response)):
        turn = await client.next_turn("system", [{"role": "user", "content": "hi"}], [])

    assert turn.usage.cache_creation_input_tokens == 0
    assert turn.usage.cache_read_input_tokens == 0


async def test_named_tool_choice_is_forwarded_to_anthropic():
    client = _client()
    with patch.object(client._client.messages, "create", new=AsyncMock(return_value=_fake_response())) as mock_create:
        await client.next_turn(
            "system",
            [{"role": "user", "content": "how many sprints?"}],
            [{"name": "query_sprints", "description": "query", "input_schema": {"type": "object"}}],
            tool_choice="query_sprints",
        )

    assert mock_create.call_args.kwargs["tool_choice"] == {
        "type": "tool",
        "name": "query_sprints",
    }
