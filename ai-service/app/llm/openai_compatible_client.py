"""Local-testing LLM client — talks to an OpenAI-compatible server (LM Studio/Ollama/vLLM).

Not a production path. It exists so the CRAG agent loop's control flow (retrieve -> assess -> maybe
re-retrieve -> answer) can be exercised end-to-end with a real, tool-calling-capable model with no
cloud API key — swap `AI_LLM_PROVIDER=anthropic` to run the real thing. This is the one place that
translates between Anthropic's native message/tool shape (which the agent loop is written against)
and OpenAI's different shape, so that translation complexity never leaks into the agent logic itself.

**Why the official `openai` SDK, not a hand-rolled `httpx` call to `/chat/completions`:** LM Studio
serves a genuinely OpenAI-compatible endpoint, so a raw `httpx.post` worked fine functionally — but
`app/tracing.py`'s OpenInference `OpenAIInstrumentor` works by patching methods on the `openai`
package's `AsyncOpenAI` class itself. Code that never touches that class produces zero spans no
matter how OpenAI-shaped its own HTTP traffic is. Routing through the real SDK (still pointed at
LM Studio's `base_url`, nothing else changes) is what makes this path show up in Phoenix at all —
before this, every locally-tested `/ask` call was invisible to tracing.
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


def _to_openai_tools(anthropic_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in anthropic_tools
    ]


def _to_openai_messages(system: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expand Anthropic-shaped messages (content = str or list of blocks) into OpenAI's flat list.

    The two formats disagree specifically on tool results: Anthropic puts them as content blocks
    inside a `user` message; OpenAI wants each as its own `{"role": "tool", ...}` message. Everything
    else (plain user/assistant text, assistant tool_use -> tool_calls) is a fairly direct mapping.
    """
    out: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
            continue

        if msg["role"] == "user":
            # A user turn's content list is either plain text blocks or tool_result blocks (never
            # both, in this agent's usage) — tool results become individual "tool" messages.
            for block in content:
                if block["type"] == "tool_result":
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": block["content"]})
                elif block["type"] == "text":
                    out.append({"role": "user", "content": block["text"]})
        else:  # assistant
            text = next((b["text"] for b in content if b["type"] == "text"), None)
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in content
                if b["type"] == "tool_use"
            ]
            entry: Dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
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
            # Newer OpenAI models (reasoning-capable) reject "max_tokens" outright ("Unsupported
            # parameter... Use 'max_completion_tokens' instead") since that budget now has to cover
            # hidden reasoning tokens too, not just the visible completion. Confirmed LM Studio accepts
            # this same newer name too (tested directly against qwen3.6-27b-mlx), so this is a strict
            # rename, not a fork per-provider.
            "max_completion_tokens": self._max_tokens,
            # Discovered live against gpt-5.6-luna: this endpoint (/v1/chat/completions) flatly
            # rejects function tools combined with any reasoning effort other than "none" ("Function
            # tools with reasoning_effort are not supported... use /v1/responses or set
            # reasoning_effort to 'none'") — not a tuning knob here, a hard requirement for tool-calling
            # to work at all on this endpoint. Confirmed harmless no-op against LM Studio (Qwen's
            # thinking toggle is the separate chat_template_kwargs mechanism, unaffected by this field).
            "reasoning_effort": "none",
            "messages": _to_openai_messages(system, messages),
            "tools": _to_openai_tools(tools),
        }
        if tool_choice:
            # LM Studio rejects OpenAI's named-tool object form ("Invalid tool_choice type:
            # 'object'"), but accepts the standard string form. Offer only the selected tool and
            # require a call: semantically this is the same named choice while remaining compatible
            # with the local provider this adapter exists to support.
            payload["tools"] = [
                tool for tool in payload["tools"]
                if tool["function"]["name"] == tool_choice
            ]
            payload["tool_choice"] = "required"

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
            retry=retry_if_exception(_is_transient_server_error),
            reraise=True,
        )
        async def _call() -> Dict[str, Any]:
            completion = await self._client.chat.completions.create(**payload)
            # Dumped to a plain dict rather than read off the typed response object: keeps every line
            # below (written against LM Studio's raw JSON shape, including its usage-field quirks)
            # unchanged — only the transport that produces this dict changed, not its shape or the
            # parsing that follows.
            return completion.model_dump()

        body = await _call()
        message = body["choices"][0]["message"]

        blocks: List[Dict[str, Any]] = []
        tool_calls: List[ToolCall] = []
        text = message.get("content") or ""
        recovered_text, recovered_calls = _parse_text_tool_calls(text)
        if recovered_calls:
            text = recovered_text
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in message.get("tool_calls") or []:
            args = json.loads(tc["function"]["arguments"] or "{}")
            blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
            tool_calls.append(ToolCall(id=tc["id"], name=tc["function"]["name"], input=args))
        for tc in recovered_calls:
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
            tool_calls.append(tc)

        # Not every OpenAI-compatible server reports usage (LM Studio does; some bare llama.cpp
        # front-ends don't) — default to 0 rather than KeyError so a missing field never breaks the
        # agent loop over a cost-visibility nicety.
        raw_usage = body.get("usage") or {}
        return LLMTurn(
            stop_reason="tool_use" if tool_calls else "end_turn",
            text=text,
            tool_calls=tool_calls,
            assistant_message={"role": "assistant", "content": blocks},
            usage=Usage(
                input_tokens=raw_usage.get("prompt_tokens", 0),
                output_tokens=raw_usage.get("completion_tokens", 0),
            ),
        )

    async def aclose(self) -> None:
        await self._client.close()
