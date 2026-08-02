"""_ReasoningContentTransport tests — fake wrapped transport, no real LM Studio/network.

Regression coverage for the live-verified finding in app/drafting/instructor_client.py: this
project's LM Studio + qwen3.6-27b-mlx combination puts response_format.json_schema-constrained JSON
into the non-standard `reasoning_content` field, never `content`, so instructor's JSON_SCHEMA mode
(which only reads `content`) would otherwise always fail to parse a perfectly valid, schema-conforming
answer.
"""
import json

import httpx

from app.drafting.instructor_client import _ReasoningContentTransport


class _FakeWrappedTransport(httpx.AsyncBaseTransport):
    def __init__(self, response: httpx.Response):
        self._response = response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._response


def _completion_response(message: dict, status_code: int = 200) -> httpx.Response:
    body = {"choices": [{"message": message}]}
    return httpx.Response(status_code, json=body)


async def test_promotes_reasoning_content_into_content_when_content_is_empty():
    inner = _completion_response({"content": "", "reasoning_content": '{"title": "Fix it"}'})
    transport = _ReasoningContentTransport(_FakeWrappedTransport(inner))

    response = await transport.handle_async_request(
        httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    )

    body = json.loads(response.content)
    assert body["choices"][0]["message"]["content"] == '{"title": "Fix it"}'


async def test_leaves_content_untouched_when_already_present():
    # A server that already answers correctly (real OpenAI/Anthropic) must be a no-op.
    inner = _completion_response({"content": '{"title": "Already fine"}', "reasoning_content": None})
    transport = _ReasoningContentTransport(_FakeWrappedTransport(inner))

    response = await transport.handle_async_request(
        httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    )

    body = json.loads(response.content)
    assert body["choices"][0]["message"]["content"] == '{"title": "Already fine"}'


async def test_ignores_non_chat_completions_requests():
    inner = httpx.Response(200, json={"unrelated": "payload"})
    transport = _ReasoningContentTransport(_FakeWrappedTransport(inner))

    response = await transport.handle_async_request(
        httpx.Request("GET", "http://localhost:1234/v1/models")
    )

    assert response is inner


async def test_ignores_non_200_responses():
    inner = httpx.Response(500, json={"error": "boom"})
    transport = _ReasoningContentTransport(_FakeWrappedTransport(inner))

    response = await transport.handle_async_request(
        httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    )

    assert response is inner


async def test_leaves_response_untouched_when_neither_field_has_content():
    inner = _completion_response({"content": "", "reasoning_content": None})
    transport = _ReasoningContentTransport(_FakeWrappedTransport(inner))

    response = await transport.handle_async_request(
        httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    )

    assert response is inner


async def test_does_not_promote_genuine_free_form_reasoning_text():
    # A "thinking" model's reasoning_content is a genuine scratch space in general — it won't always
    # be the schema-constrained answer that landed in the wrong field. Prose like this must NOT be
    # promoted into `content`: that would hand instructor's JSON parser garbage. Left alone, content
    # stays empty and the caller gets the same validation-failure -> retry -> degrade outcome as if
    # this transport didn't exist, never a wrong-but-plausible-looking structured answer.
    inner = _completion_response({
        "content": "",
        "reasoning_content": "Let me think about this... the user wants a task about the checkout "
                              "bug, so I should draft something urgent.",
    })
    transport = _ReasoningContentTransport(_FakeWrappedTransport(inner))

    response = await transport.handle_async_request(
        httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    )

    assert response is inner
