"""VLM (vision-language model) fallback for image attachments EasyOCR struggled with.

Same rationale structure as `app/ingest/contextualizer.py` (off by default, real LLM cost, two
provider backends) applied to a different problem: `docling_parser.py`'s EasyOCR path does classic
OCR — detect text regions, read characters, no understanding of what the text means — which is fast
and free but has a specific, real failure mode documented in that module: on a degraded
(rotated/blurred/low-contrast/re-compressed) photo, EasyOCR doesn't fail loudly, it produces a
*confident-looking wrong token*
("Mx" as its read of a mangled "SKU:"+"Pallet" region) that a downstream agent then presents as a real
value. A confidence threshold filters the worst of that, but doesn't recover the value — it just turns
"wrong" into "missing."

A vision-language model sees the whole image at once rather than character-by-character, so it can do
what OCR structurally cannot: decide a region is illegible and *say so* instead of emitting its best
guess at a character sequence. This is the actual reason "OCR often fails at hard documents"
increasingly means "use a VLM" in production RAG systems (LlamaParse and Unstructured.io have both
moved this direction) — not that VLMs are magic, but that "I can't read this" is a real output option
for them and not for character-level OCR.

Off by default (`VEC_VLM_OCR_ENABLED=false`): a real LLM call per escalated image, same cost argument
`contextual_retrieval_enabled` already makes. Only escalates (see `docling_parser.py`'s
`_maybe_escalate_to_vlm`) when EasyOCR showed a concrete sign of struggling — not run on every image
unconditionally, since most attachment images in this project's corpus are clean synthetic screenshots
EasyOCR already handles correctly (see Case Study 6).
"""
from __future__ import annotations

import base64
import logging
from typing import Protocol

import anthropic
import httpx

logger = logging.getLogger(__name__)

# Explicitly tells the model to mark illegible regions rather than guess — the entire point of using
# a VLM here is recovering the "I can't read this" option EasyOCR's character-level matching doesn't
# have. A prompt that just said "transcribe this image" would reproduce OCR's exact failure mode.
_VLM_PROMPT_VERSION = "document-extraction-v2"

_VLM_PROMPT = """Extract high-fidelity, searchable reference content from this attachment image. The \
result will be embedded in a RAG system, so accuracy, source fidelity, and preserving structure matter \
more than prose quality.

Rules:
- Transcribe text you can read with real confidence exactly as written, including numbers, codes, \
and units.
- If part of the image is blurry, rotated, low-contrast, or otherwise illegible, write \
"[illegible: <what field or region this appears to be>]" for that part instead of guessing a plausible \
-looking value. A wrong value is worse than an honest gap — never invent one.
- If the image contains a table or a list of label/value pairs, preserve that structure as one \
"label: value" line per row.
- Render tables as Markdown when their rows and columns are readable. Do not invent missing cells.
- For a chart, diagram, workflow, or other visual with factual meaning not captured by its text, add a \
short `Visual summary:` describing only directly observable entities, relationships, labels, values, \
and trends. Do not infer causes, intent, or values that are not visible.
- Keep headings and sections in reading order. Do not add introductions or conclusions.
- If literally no text is visible anywhere in the image, respond with exactly: (no text found)"""


class VlmImageDescriber(Protocol):
    async def describe(self, image_data: bytes, mime_type: str) -> str: ...

    async def aclose(self) -> None: ...


class NoopVlmImageDescriber:
    """Used when VLM escalation is disabled — never calls out, always returns ''."""

    async def describe(self, image_data: bytes, mime_type: str) -> str:
        return ""

    async def aclose(self) -> None:
        return None


class AnthropicVlmImageDescriber:
    def __init__(self, api_key: str, model: str, max_completion_tokens: int = 500):
        self._client = anthropic.AsyncAnthropic(api_key=api_key or None)
        self._model = model
        self._max_completion_tokens = max_completion_tokens

    async def describe(self, image_data: bytes, mime_type: str) -> str:
        b64 = base64.b64encode(image_data).decode()
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_completion_tokens,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                    {"type": "text", "text": _VLM_PROMPT},
                ],
            }],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()

    async def aclose(self) -> None:
        await self._client.close()


class OpenAICompatibleVlmImageDescriber:
    """Real production path for this project — points at a hosted vision-capable model (e.g.
    gpt-5.6-luna) via the same OpenAI-compatible chat/completions shape `instructor_client.py` and
    `contextualizer.py` already use, just with an `image_url` content part added.
    """

    def __init__(self, base_url: str, api_key: str, model: str, max_completion_tokens: int = 500):
        self._model = model
        self._max_completion_tokens = max_completion_tokens
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=60.0
        )

    async def describe(self, image_data: bytes, mime_type: str) -> str:
        b64 = base64.b64encode(image_data).decode()
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                # `max_completion_tokens`, not `max_tokens`: found live against this project's own
                # configured production model (gpt-5.6-luna) — see
                # ai-service/app/drafting/instructor_client.py's `_rewrite_max_tokens` for the first
                # occurrence of this same bug (Case Study 28) and Case Study 30 for this module's.
                # ai-service fixes it via an httpx transport shared by every instructor call; this
                # module has no such shared transport (it isn't an instructor client), so the request
                # is just built correctly to begin with.
                "max_completion_tokens": self._max_completion_tokens,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VLM_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                    ],
                }],
            },
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()


def build_vlm_describer(settings) -> VlmImageDescriber:
    if not settings.vlm_ocr_enabled:
        return NoopVlmImageDescriber()
    if settings.vlm_provider == "anthropic":
        return AnthropicVlmImageDescriber(
            settings.vlm_anthropic_api_key,
            settings.vlm_anthropic_model,
            settings.vlm_max_completion_tokens,
        )
    if settings.vlm_provider == "openai_compatible":
        return OpenAICompatibleVlmImageDescriber(
            settings.vlm_openai_base_url,
            settings.vlm_openai_api_key,
            settings.vlm_openai_model,
            settings.vlm_max_completion_tokens,
        )
    raise ValueError(f"unknown vlm_provider: {settings.vlm_provider}")


def vlm_cache_namespace(settings) -> str:
    """Version all inputs that can change extraction output before using a durable cache entry."""
    model = settings.vlm_anthropic_model if settings.vlm_provider == "anthropic" else settings.vlm_openai_model
    return f"{_VLM_PROMPT_VERSION}:{settings.vlm_provider}:{model}:{settings.vlm_max_completion_tokens}"
