"""Tests for app/ingest/vlm_describer.py — no real network, mocked httpx/anthropic transports, same
approach as tests/test_contextual_retrieval.py uses for the analogous contextualizer.py.
"""
import json

import httpx
import pytest

from app.config import Settings
from app.ingest.vlm_describer import (
    NoopVlmImageDescriber,
    OpenAICompatibleVlmImageDescriber,
    build_vlm_describer,
)


async def test_noop_describer_never_calls_out_and_returns_empty():
    describer = NoopVlmImageDescriber()
    assert await describer.describe(b"fake image bytes", "image/png") == ""
    await describer.aclose()  # must not raise


async def test_build_vlm_describer_returns_noop_when_disabled():
    settings = Settings(vlm_ocr_enabled=False)
    describer = build_vlm_describer(settings)
    assert isinstance(describer, NoopVlmImageDescriber)


async def test_build_vlm_describer_rejects_an_unknown_provider():
    settings = Settings(vlm_ocr_enabled=True, vlm_provider="not-a-real-provider")
    with pytest.raises(ValueError, match="unknown vlm_provider"):
        build_vlm_describer(settings)


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, response_body: dict, status_code: int = 200):
        self._body = response_body
        self._status = status_code
        self.sent_requests = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent_requests.append(request)
        return httpx.Response(self._status, json=self._body)


async def test_openai_compatible_describer_sends_the_image_as_a_data_uri_and_returns_the_content():
    fake = _FakeTransport({"choices": [{"message": {"content": "SKU: W-7734"}}]})
    describer = OpenAICompatibleVlmImageDescriber("http://localhost:1234/v1", "local", "some-vlm")
    describer._client = httpx.AsyncClient(
        base_url="http://localhost:1234/v1", transport=fake
    )

    result = await describer.describe(b"\x89PNG raw bytes", "image/png")

    assert result == "SKU: W-7734"
    sent = json.loads(fake.sent_requests[0].content)
    assert sent["model"] == "some-vlm"
    image_part = next(p for p in sent["messages"][0]["content"] if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    await describer.aclose()


async def test_openai_compatible_describer_sends_max_completion_tokens_not_max_tokens():
    # Regression test for a real bug hit live (the second occurrence of it in this project — see
    # ai-service's Case Study 28): this project's own configured production model (gpt-5.6-luna)
    # rejects the older `max_tokens` param with a 400. This module builds its own httpx client (it
    # isn't an instructor call, so it doesn't get instructor_client.py's shared rewrite transport),
    # so the request has to use the right name from the start.
    fake = _FakeTransport({"choices": [{"message": {"content": "x"}}]})
    describer = OpenAICompatibleVlmImageDescriber(
        "http://localhost:1234/v1", "local", "some-vlm", max_completion_tokens=2_000
    )
    describer._client = httpx.AsyncClient(base_url="http://localhost:1234/v1", transport=fake)

    await describer.describe(b"bytes", "image/png")

    sent = json.loads(fake.sent_requests[0].content)
    assert "max_tokens" not in sent
    assert sent["max_completion_tokens"] == 2_000
    await describer.aclose()


async def test_openai_compatible_describer_raises_on_a_non_200_so_the_caller_can_fall_back():
    # docling_parser.py's _maybe_escalate_to_vlm relies on this raising rather than silently
    # returning "" — an HTTP error must be distinguishable from "the model legitimately found no text."
    fake = _FakeTransport({"error": "server error"}, status_code=500)
    describer = OpenAICompatibleVlmImageDescriber("http://localhost:1234/v1", "local", "some-vlm")
    describer._client = httpx.AsyncClient(base_url="http://localhost:1234/v1", transport=fake)

    with pytest.raises(httpx.HTTPStatusError):
        await describer.describe(b"bytes", "image/png")
    await describer.aclose()
