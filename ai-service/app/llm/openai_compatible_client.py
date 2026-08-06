"""Local-testing LLM client — talks to an OpenAI-compatible server (LM Studio/Ollama/vLLM) or a real
hosted OpenAI-family model, over the Responses API.

Not a production path (the production path is `anthropic_client.py`). It exists so the CRAG agent
loop's control flow (retrieve -> assess -> maybe re-retrieve -> answer) can be exercised end-to-end
with a real, tool-calling-capable model with no cloud API key — swap `AI_LLM_PROVIDER=anthropic` to
run the real thing. This is the one place that translates between Anthropic's native message/tool
shape (which the agent loop is written against) and OpenAI's different shape, so that translation
complexity never leaks into the agent logic itself.

**Why the official `openai` SDK, not a hand-rolled `httpx` call:** LM Studio serves a genuinely
OpenAI-compatible endpoint, so a raw `httpx.post` worked fine functionally — but `app/tracing.py`'s
OpenInference `OpenAIInstrumentor` works by patching methods on the `openai` package's `AsyncOpenAI`
class itself (confirmed it also covers `.responses.create`, not just `.chat.completions.create`).
Code that never touches that class produces zero spans no matter how OpenAI-shaped its own HTTP
traffic is. Routing through the real SDK (still pointed at LM Studio's `base_url`, nothing else
changes) is what makes this path show up in Phoenix at all.

**Why `/v1/responses`, not `/v1/chat/completions`:** discovered live testing `gpt-5.6-luna` — that
endpoint flatly rejects function tools combined with any reasoning effort other than `"none"`
("Function tools with reasoning_effort are not supported... use /v1/responses or set reasoning_effort
to 'none'"). Forcing `"none"` got tool-calling working but meant every call ran with reasoning
disabled entirely, even for the tool-selection judgment calls that could benefit from it (the local
Qwen path genuinely does reason during `tool_calls`-finishing turns, observed directly via Phoenix
traces — reasoning during tool selection is real judgment, not wasted tokens, contrary to what the
old endpoint's restriction implied). `/v1/responses` supports reasoning and
function tools together, and LM Studio added compatible support for it (July 2026, same Open
Responses spec) — so one implementation now serves both providers, and both actually get to combine
the two instead of being forced to pick.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import openai
from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.llm.types import LLMTurn, ToolCall, Usage


_TEXT_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_XML_FUNCTION_RE = re.compile(
    r"<function=([A-Za-z_][A-Za-z0-9_]*)>\s*(.*?)\s*</function>",
    re.DOTALL | re.IGNORECASE,
)
_XML_PARAMETER_RE = re.compile(
    r"<parameter=([A-Za-z_][A-Za-z0-9_]*)>\s*(.*?)\s*</parameter>",
    re.DOTALL | re.IGNORECASE,
)


def _parse_scalar(value: str) -> Any:
    """Parse JSON-shaped parameter values while leaving ordinary model text as a string."""
    value = value.strip()
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _parse_text_tool_calls(text: str) -> tuple[str, List[ToolCall]]:
    """Recover tool calls that a local model emitted in its text channel.

    Some OpenAI-compatible servers fail to translate the model's native ``<tool_call>`` syntax into
    ``message.tool_calls``. Treating that payload as an end-user answer leaks internal protocol
    markup into the UI and, more importantly, means the requested tool is never executed. Support
    the two common encodings produced by local instruct models: an XML-like function/parameter shape
    and a JSON object inside ``<tool_call>``. Only complete, parseable blocks are removed from text;
    malformed markup remains visible for diagnostics rather than being silently discarded.
    """
    calls: List[ToolCall] = []
    consumed_spans: List[tuple[int, int]] = []

    for index, match in enumerate(_TEXT_TOOL_CALL_RE.finditer(text), start=1):
        payload = match.group(1).strip()
        name: str | None = None
        arguments: Dict[str, Any] | None = None

        xml_match = _XML_FUNCTION_RE.fullmatch(payload)
        if xml_match:
            name = xml_match.group(1)
            arguments = {
                parameter.group(1): _parse_scalar(parameter.group(2))
                for parameter in _XML_PARAMETER_RE.finditer(xml_match.group(2))
            }
        else:
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                function = decoded.get("function")
                if isinstance(function, dict):
                    name = function.get("name")
                    raw_arguments = function.get("arguments", {})
                else:
                    name = decoded.get("name")
                    raw_arguments = decoded.get("arguments", decoded.get("input", {}))
                if isinstance(raw_arguments, str):
                    try:
                        raw_arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        raw_arguments = None
                if isinstance(raw_arguments, dict):
                    arguments = raw_arguments

        if name and arguments is not None:
            calls.append(ToolCall(id=f"text_tool_call_{index}", name=name, input=arguments))
            consumed_spans.append(match.span())

    if not consumed_spans:
        return text, []

    pieces: List[str] = []
    cursor = 0
    for start, end in consumed_spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    cleaned = "".join(pieces).strip()
    return cleaned, calls


def _is_transient_server_error(exc: BaseException) -> bool:
    # A single local model server (LM Studio/Ollama) handling one request at a time will 5xx a
    # request that arrives while it's already busy with another — observed directly under the
    # concurrency load test (loadtest/locustfile_ai_service.py; see loadtest/README.md), not a
    # theoretical case. That's a transient "try again shortly" condition, unlike a 4xx (this
    # client sent a malformed request), which retrying would never fix — so only 5xx is retried.
    # The `openai` SDK raises `APIStatusError` subclasses (`InternalServerError` for 5xx) instead of
    # `httpx.HTTPStatusError` now that requests go through it — same distinction, different exception type.
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


def _to_responses_tools(anthropic_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Responses API tools are flat (name/description/parameters at the top level) — unlike Chat
    # Completions, which nests them under a "function" key. Not a stylistic choice on our part, just
    # matching the wire format this endpoint actually expects.
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in anthropic_tools
    ]


def _to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expand Anthropic-shaped messages (content = str or list of blocks) into the Responses API's
    flat `input` item list.

    The system prompt is NOT part of this list — Responses API takes it separately as a top-level
    `instructions` field (see `next_turn`). The other structural difference from Chat Completions:
    tool calls/results aren't message roles here, they're their own item types —
    `function_call` (an assistant turn's tool invocation) and `function_call_output` (its result),
    correlated by `call_id` rather than nested inside a message.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
            continue

        if msg["role"] == "user":
            # A user turn's content list is either plain text blocks or tool_result blocks (never
            # both, in this agent's usage).
            for block in content:
                if block["type"] == "tool_result":
                    out.append({
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": block["content"],
                    })
                elif block["type"] == "text":
                    out.append({"role": "user", "content": block["text"]})
        else:  # assistant
            text = next((b["text"] for b in content if b["type"] == "text"), None)
            if text:
                out.append({"role": "assistant", "content": text})
            for b in content:
                if b["type"] == "tool_use":
                    out.append({
                        "type": "function_call",
                        "call_id": b["id"],
                        "name": b["name"],
                        "arguments": json.dumps(b["input"]),
                    })
    return out


class OpenAICompatibleClient:
    def __init__(self, base_url: str, api_key: str, model: str, max_output_tokens: int, request_timeout_seconds: float = 300.0):
        self._model = model
        self._max_tokens = max_output_tokens
        self._client = AsyncOpenAI(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout=request_timeout_seconds,
        )

    async def next_turn(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: str | None = None,
    ) -> LLMTurn:
        payload = {
            "model": self._model,
            "instructions": system,
            "input": _to_responses_input(messages),
            "tools": _to_responses_tools(tools),
            # Responses API's field for the output token budget — "max_output_tokens", not Chat
            # Completions' "max_tokens"/"max_completion_tokens".
            "max_output_tokens": self._max_tokens,
            # Now that tools and reasoning can coexist on this endpoint (the reason for being on
            # /v1/responses at all — see module docstring), default to a light reasoning budget rather
            # than force it off: tool-selection judgment is a real reasoning task (observed directly —
            # the local Qwen path reasons during tool_calls turns), "low" gets some of that benefit
            # without the verbose, mostly-wasted chain-of-thought a narrow classification call would
            # otherwise burn through.
            "reasoning": {"effort": "low"},
        }
        if tool_choice:
            # Offer only the selected tool and require a call — same reasoning as the old Chat
            # Completions path: a plain string form is the most broadly compatible way to express a
            # forced single-tool choice.
            payload["tools"] = [
                tool for tool in payload["tools"]
                if tool["name"] == tool_choice
            ]
            payload["tool_choice"] = "required"

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
            retry=retry_if_exception(_is_transient_server_error),
            reraise=True,
        )
        async def _call() -> Dict[str, Any]:
            response = await self._client.responses.create(**payload)
            return response.model_dump()

        body = await _call()

        blocks: List[Dict[str, Any]] = []
        tool_calls: List[ToolCall] = []
        text_parts: List[str] = []
        # NOTE: the SDK's `response.output_text` is a client-side *property* that aggregates message
        # text (see openai.types.responses.Response.output_text) — it does not survive
        # `.model_dump()`, which only serializes actual fields. Confirmed live (returned "" against a
        # real multi-turn call that plainly had a text answer). Walking `output` ourselves avoids
        # depending on that SDK-side convenience at all, consistent with this file's existing "dump to
        # a plain dict, parse it explicitly" approach for every other field.
        for item in body.get("output", []):
            item_type = item.get("type")
            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text" and part.get("text"):
                        text_parts.append(part["text"])
            elif item_type == "function_call":
                args = json.loads(item.get("arguments") or "{}")
                call_id = item["call_id"]
                blocks.append({"type": "tool_use", "id": call_id, "name": item["name"], "input": args})
                tool_calls.append(ToolCall(id=call_id, name=item["name"], input=args))
            # "reasoning" items carry no visible content on hosted OpenAI models (LM Studio does
            # surface reasoning text, but this agent has no use for it) — skipped either way.
        text = "".join(text_parts)

        recovered_text, recovered_calls = _parse_text_tool_calls(text)
        if recovered_calls:
            text = recovered_text
        if text:
            blocks.insert(0, {"type": "text", "text": text})
        for tc in recovered_calls:
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
            tool_calls.append(tc)

        # Not every OpenAI-compatible server reports usage — default to 0 rather than KeyError so a
        # missing field never breaks the agent loop over a cost-visibility nicety.
        raw_usage = body.get("usage") or {}
        return LLMTurn(
            stop_reason="tool_use" if tool_calls else "end_turn",
            text=text,
            tool_calls=tool_calls,
            assistant_message={"role": "assistant", "content": blocks},
            usage=Usage(
                input_tokens=raw_usage.get("input_tokens", 0),
                output_tokens=raw_usage.get("output_tokens", 0),
            ),
        )

    async def aclose(self) -> None:
        await self._client.close()
