"""The production LLM client — the official `anthropic` SDK, default model `claude-opus-4-8`.

Non-streaming: `max_output_tokens` defaults to 2048, well under the ~16K threshold where the SDK
starts risking HTTP timeouts, so `messages.create()` is fine here. Thinking is deliberately left off
for this first version — a CRAG agent that reasons about retrieval sufficiency would benefit from
adaptive thinking, but enabling it means preserving thinking blocks verbatim across turns, which adds
real complexity to a loop that's meant to be walked through and understood end-to-end. Noted as the
natural next enhancement, not an oversight.

**Prompt caching.** Every call in the CRAG loop resends the full system prompt + tool schemas, plus —
once conversation history (app/agent/crag_loop.py's `ask(..., history=...)`) or a multi-round tool
loop is in play — a growing `messages` array. Without caching that's paid, full-price input tokens on
every single call, cost scaling with both tool-loop rounds and conversation length. Two cache
breakpoints (Anthropic allows up to 4 per request) cover both:

  1. On the system prompt — constant across EVERY call (same 5-minute-rounded prompt from
     `crag_loop._current_time_for_prompt`), so it can be shared across tool-loop rounds, across
     turns in one conversation, and across different conversations/users within the same window.
  2. On the last block of the last message — caches everything up to and including "the conversation
     so far," so the next call in the same tool loop (or the next turn, if the caller passed history)
     only pays full price for what's actually new.

First use of a given prefix is a cache *write* (billed ~1.25x normal input — a premium to make it
available); a subsequent identical-prefix call is a cache *read* (~0.1x normal input, a ~90%
discount). Net savings appear from the second call sharing a prefix onward, not the first. See
app/llm/pricing.py for the exact multipliers and app/llm/types.py::Usage for why the two cache token
counts are tracked separately from `input_tokens` rather than folded in — they're priced differently
and Anthropic reports them as distinct usage fields.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

import anthropic

from app.llm.types import LLMTurn, ToolCall, Usage

_CACHE_CONTROL = {"cache_control": {"type": "ephemeral"}}


def _cached_system(system: str) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": system, **_CACHE_CONTROL}]


def _with_trailing_cache_breakpoint(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a NEW list, deep-copying only the last message, with cache_control on its last content
    block. Never mutates the caller's list — `messages` is the agent loop's own running conversation
    state, reused (and appended to) across every remaining round of this same ask() call, so a stray
    cache_control marker baked into it would linger into turns that never intended to place one there.
    """
    if not messages:
        return messages
    *head, last = messages
    last = copy.deepcopy(last)
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, **_CACHE_CONTROL}]
    elif isinstance(content, list) and content:
        content[-1] = {**content[-1], **_CACHE_CONTROL}
    return [*head, last]


class AnthropicClient:
    def __init__(self, api_key: str, model: str, max_output_tokens: int):
        # Empty api_key falls through to the SDK's own env/profile resolution (ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile) rather than forcing a bad empty key.
        self._client = anthropic.AsyncAnthropic(api_key=api_key or None)
        self._model = model
        self._max_tokens = max_output_tokens

    async def next_turn(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: str | None = None,
    ) -> LLMTurn:
        request = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            system=_cached_system(system),
            messages=_with_trailing_cache_breakpoint(messages),
            tools=tools,
        )
        if tool_choice:
            request["tool_choice"] = {"type": "tool", "name": tool_choice}
        response = await self._client.messages.create(**request)

        blocks: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                blocks.append({"type": "text", "text": block.text})
                text_parts.append(block.text)
            elif block.type == "tool_use":
                blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        stop_reason = "tool_use" if response.stop_reason == "tool_use" else "end_turn"
        return LLMTurn(
            stop_reason=stop_reason,
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            assistant_message={"role": "assistant", "content": blocks},
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                # Absent entirely on SDK responses where caching wasn't in play (older account/API
                # version); getattr keeps that a clean 0 rather than an AttributeError.
                cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            ),
        )

    async def aclose(self) -> None:
        await self._client.close()
