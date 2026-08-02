"""Provider-agnostic LLM types.

The CRAG agent loop (app/agent/crag_loop.py) is written once against these types and Anthropic's
native Messages-API wire shape — content blocks as plain dicts (``{"type": "text", "text": ...}``,
``{"type": "tool_use", "id", "name", "input"}``, ``{"type": "tool_result", "tool_use_id", "content"}``)
— because that shape IS the production target. ``AnthropicClient`` (app/llm/anthropic_client.py) is
close to a pass-through. ``OpenAICompatibleClient`` (app/llm/openai_compatible_client.py) is the one
place that translates to/from OpenAI's different message/tool-call shape, so the adapter complexity
is isolated to the local-testing path and never leaks into the agent logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Protocol

Role = Literal["user", "assistant"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass(slots=True)
class Usage:
    """Token counts for one LLM call, normalized across providers for cost tracking (app/llm/pricing.py).

    Some OpenAI-compatible local servers don't report usage at all (LM Studio does; a bare llama.cpp
    server might not) — both fields default to 0 rather than the call failing, since token counting
    is a cost-visibility feature, not something that should ever break the agent loop.

    ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` are Anthropic-specific (prompt
    caching — app/llm/anthropic_client.py) and always 0 for any other provider. Kept as separate
    counters rather than folded into ``input_tokens`` because they're priced differently (see
    app/llm/pricing.py) — collapsing them would make cost estimation silently wrong the moment
    caching is enabled.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


@dataclass(slots=True)
class LLMTurn:
    """One assistant turn, normalized across providers."""

    stop_reason: Literal["tool_use", "end_turn"]
    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    # The exact {"role": "assistant", "content": [...]} dict to append to `messages` for the next
    # call — content blocks are plain dicts in Anthropic's native shape (see module docstring).
    assistant_message: Dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)


class LLMClient(Protocol):
    async def next_turn(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: str | None = None,
    ) -> LLMTurn: ...

    async def aclose(self) -> None: ...
