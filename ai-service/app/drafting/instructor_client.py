"""Builds an `instructor`-patched client for structured task drafting.

Separate from `app/llm/factory.py`'s `LLMClient` (the hand-rolled tool-calling abstraction the CRAG
agent loop is built on): that one is written directly against Anthropic's native message/tool shape
because the agent loop's own control flow (grade retrieval, decide to re-search) needs to inspect
`stop_reason` and tool-call arguments directly. Structured task drafting has no control flow to
inspect — it's one call in, one validated Pydantic object out — which is exactly `instructor`'s job:
it wraps the underlying provider SDK, validates the response against a Pydantic model, and retries
automatically (re-prompting with the validation error) if the first attempt doesn't parse. Using it
here instead of hand-rolling a second tool-calling parser avoids duplicating logic `instructor`
already solves well.

Mode differs by provider, found by testing directly against this project's actual local model
(`qwen3.6-27b-mlx` via LM Studio) rather than assumed from documentation:
  * Anthropic: `Mode.ANTHROPIC_TOOLS` — the documented, standard approach; Anthropic's tool-use is
    robust enough not to need a fallback mode. Not yet run live in this project (no
    `ANTHROPIC_API_KEY` configured here — same caveat as `app/llm/anthropic_client.py`).
  * OpenAI-compatible: `Mode.JSON_SCHEMA` — sends the standard `response_format: {"type":
    "json_schema", ...}` structured-outputs parameter. Earlier revisions of this module used
    `Mode.MD_JSON` (ask the model to return JSON in a markdown code block, no schema enforcement)
    because `Mode.TOOLS` (object-shaped `tool_choice`) and `Mode.JSON` (`response_format:
    {"type": "json_object"}`) both failed against this project's LM Studio server — confirmed
    still true (`Mode.TOOLS`'s object `tool_choice` still 400s: "Invalid tool_choice type:
    'object'. Supported string values: none, auto, required"). But `response_format.json_schema`
    (`Mode.JSON_SCHEMA`) is now accepted (LM Studio added support since MD_JSON was chosen), giving
    real API-enforced schema constraints instead of just asking nicely in the prompt.
  * One live-verified wrinkle specific to `qwen3.6-27b-mlx` (a "thinking" model): with
    `response_format.json_schema`, this LM Studio server always puts the schema-constrained JSON in
    the response's non-standard `reasoning_content` field, never in the standard `content` field
    that `instructor`/the OpenAI SDK read — confirmed unaffected by every thinking-mode toggle tried
    (`chat_template_kwargs.enable_thinking=False`, top-level `enable_thinking=False`, a `/no_think`
    prompt suffix). `_ReasoningContentTransport` below is the fix: an httpx transport that promotes
    `reasoning_content` into `content` when `content` is empty, before the SDK ever parses the
    response. No-op against any server that already answers in `content` (real OpenAI, Anthropic).

    Promotion only fires when `reasoning_content` itself parses as JSON — `reasoning_content` is a
    model's genuine scratch space for a "thinking" model in general (this project observed it
    containing ONLY the schema-constrained answer for this specific model/server combination, never
    mixed with free-form chain-of-thought, but that's not guaranteed to hold for every model this
    branch might ever run against). Blindly promoting non-JSON prose into `content` would hand
    instructor's parser garbage instead of nothing; requiring it to parse as JSON first means a
    genuine chain-of-thought blob is left alone and falls through to the exact same "empty content"
    outcome (a validation failure, then instructor's reask-and-retry, then the caller's safe
    degradation) this transport didn't exist to change in the first place.
"""
from __future__ import annotations

import json
from typing import Tuple

import httpx
import instructor

from app.config import Settings


def _rewrite_max_tokens(request: httpx.Request) -> httpx.Request:
    """Renames `max_tokens` to `max_completion_tokens` on outgoing /chat/completions requests.

    Found live, not by inspection: with this project configured against its real hosted production
    model (`AI_LLM_PROVIDER=openai_compatible`, `AI_AGENT_MODEL=gpt-5.6-luna`,
    `AI_OPENAI_BASE_URL=https://api.openai.com/v1`), *every* structured-output feature — Plan Epic,
    Draft Task, Sprint Health — was returning a 400 ("Unsupported parameter: 'max_tokens' is not
    supported with this model. Use 'max_completion_tokens' instead.") and silently falling back to its
    degraded path. Nothing crashed and nothing 500'd, because each of those callers is deliberately
    written to degrade rather than fail — which is correct behaviour that also happened to hide a
    total feature outage behind a `degraded=True` flag nobody was watching.

    Fixed here rather than at the four call sites because the parameter name is a property of the
    *wire protocol*, not of any one feature: `max_completion_tokens` is OpenAI's current name for it
    (`max_tokens` is deprecated there and outright rejected by reasoning models), and this transport
    is only ever installed on the OpenAI-compatible path — the Anthropic client, whose API genuinely
    uses `max_tokens`, never sees it. Verified against both servers this project actually runs
    against: hosted OpenAI (which requires the new name) and LM Studio (which accepts it), so one
    unconditional rule covers both rather than a base-URL sniff.
    """
    if "chat/completions" not in str(request.url):
        return request
    try:
        body = json.loads(request.content)
    except (json.JSONDecodeError, ValueError):
        return request
    if "max_tokens" not in body:
        return request

    body["max_completion_tokens"] = body.pop("max_tokens")
    content = json.dumps(body).encode()
    return httpx.Request(
        method=request.method,
        url=request.url,
        # Drop content-length: the rewritten body is a different byte length and a stale header would
        # truncate the request (same reasoning as the response rewrite below).
        headers={k: v for k, v in request.headers.items() if k.lower() != "content-length"},
        content=content,
        extensions=request.extensions,
    )


class _ReasoningContentTransport(httpx.AsyncBaseTransport):
    """See module docstring's "live-verified wrinkle" — promotes a thinking model's
    `reasoning_content` into the standard `content` field when `content` came back empty, so
    `instructor`'s JSON_SCHEMA-mode parsing (which only ever looks at `content`) actually sees the
    schema-constrained output. Only rewrites `/chat/completions` responses; anything else (or a
    non-200, or a response that already has `content`) passes through untouched.

    Also rewrites the *request*: `max_tokens` -> `max_completion_tokens` — see
    `_rewrite_max_tokens` for why that has to happen here rather than at each call site.
    """

    def __init__(self, wrapped: httpx.AsyncBaseTransport):
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request = _rewrite_max_tokens(request)
        response = await self._wrapped.handle_async_request(request)
        if "chat/completions" not in str(request.url) or response.status_code != 200:
            return response
        await response.aread()
        try:
            body = json.loads(response.content)
        except json.JSONDecodeError:
            return response

        changed = False
        for choice in body.get("choices", []):
            message = choice.get("message", {})
            if message.get("content"):
                continue  # already has a real answer — never overwrite it, promoted or not
            reasoning = message.get("reasoning_content")
            if not reasoning:
                continue
            try:
                json.loads(reasoning)
            except (json.JSONDecodeError, TypeError):
                # Not parseable JSON — this is the model's genuine free-form chain-of-thought, not
                # a schema-constrained answer that landed in the wrong field. Promoting prose into
                # `content` would feed instructor's JSON parser garbage instead of nothing; leaving
                # `content` empty produces the exact same downstream outcome (a validation failure
                # that triggers instructor's reask-and-retry, then the caller's safe degradation) as
                # it would have without this transport at all — so skipping here costs nothing and
                # avoids ever passing off real reasoning text as a structured answer.
                continue
            message["content"] = reasoning
            changed = True
        if not changed:
            return response

        return httpx.Response(
            status_code=response.status_code,
            # Drop content-length: the rewritten body is a different byte length, and a stale
            # header would make httpx/the SDK truncate or misread the response.
            headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
            content=json.dumps(body).encode(),
            request=request,
        )


def build_instructor_client(settings: Settings) -> Tuple[instructor.AsyncInstructor, str]:
    """Returns (client, model) — model is separate because the two providers name models differently."""
    if settings.llm_provider == "anthropic":
        import anthropic

        client = instructor.from_anthropic(
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None),
            mode=instructor.Mode.ANTHROPIC_TOOLS,
        )
        return client, settings.agent_model

    import openai

    http_client = httpx.AsyncClient(transport=_ReasoningContentTransport(httpx.AsyncHTTPTransport()))
    client = instructor.from_openai(
        openai.AsyncOpenAI(
            base_url=settings.openai_base_url, api_key=settings.openai_api_key, http_client=http_client
        ),
        mode=instructor.Mode.JSON_SCHEMA,
    )
    return client, settings.agent_model
