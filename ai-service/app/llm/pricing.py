"""Static $/token pricing, used to turn raw token counts into an estimated cost per request.

This is deliberately a hardcoded table, not a live pricing API call: Anthropic doesn't expose one,
prices change rarely enough that a hardcoded table is the industry-normal approach (it's what cost
dashboards in most LLM observability tools — Langfuse, Helicone, etc. — do internally), and a stale
price is a bounded, self-correcting error (update the table), not a silent failure mode.

Local/self-hosted models (LM Studio, Ollama, vLLM) have no per-token API cost — running them costs
electricity and amortized hardware, not a metered bill — so they price at $0/token here. Token counts
are still tracked for those models (see app/llm/types.py Usage) because token volume is still a
useful capacity-planning signal even when it's free.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float

    # Anthropic prompt-caching multipliers on the base input price — documented as fixed ratios
    # across every model, not a separate per-model number: writing a new cache entry costs 1.25x
    # normal input (you pay a premium to have it available for the next call); reading a cache hit
    # costs 0.1x (a ~90% discount on the tokens that hit). Computed as properties, not stored fields,
    # so there's exactly one number (input_per_million) to keep in sync with Anthropic's price sheet.
    @property
    def cache_write_per_million(self) -> float:
        return self.input_per_million * 1.25

    @property
    def cache_read_per_million(self) -> float:
        return self.input_per_million * 0.1


# $ per 1M tokens. Source: Anthropic's published pricing (see claude-api skill, "Current Models").
_ANTHROPIC_PRICING = {
    "claude-fable-5": ModelPrice(10.00, 50.00),
    "claude-mythos-5": ModelPrice(10.00, 50.00),
    "claude-opus-4-8": ModelPrice(5.00, 25.00),
    "claude-opus-4-7": ModelPrice(5.00, 25.00),
    "claude-opus-4-6": ModelPrice(5.00, 25.00),
    "claude-sonnet-5": ModelPrice(3.00, 15.00),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
}

# $ per 1M tokens, cross-checked directly against OpenAI's own pricing page (Standard tier, short
# context) on 2026-07-29 — supersedes the earlier gpt-4o/gpt-4o-mini placeholder, which was never
# checked against a live page and is no longer even listed there (superseded by the gpt-5.x family).
# This project has still never actually billed against these (see AnthropicClient/
# OpenAICompatibleClient docstrings — neither cloud provider has been run live here yet), so treat as
# current-as-of-check pricing, not a verified-by-real-spend number. Reached via
# AI_LLM_PROVIDER=openai_compatible pointed at a real AI_OPENAI_BASE_URL (https://api.openai.com/v1)
# rather than a local LM Studio/Ollama/vLLM server — same client code path, see
# app/llm/openai_compatible_client.py's module docstring. Long-context and Batch/Flex/Priority tiers
# exist too (roughly 2x short-context input/output for long context; Batch is ~50% of Standard) but
# aren't modeled here — this project's calls are short-context, interactive (not batch) traffic.
_OPENAI_PRICING = {
    "gpt-5.6-sol": ModelPrice(5.00, 30.00),
    "gpt-5.6-terra": ModelPrice(2.50, 15.00),
    "gpt-5.6-luna": ModelPrice(1.00, 6.00),
    "gpt-5.5": ModelPrice(5.00, 30.00),
    "gpt-5.5-pro": ModelPrice(30.00, 180.00),
    "gpt-5.4": ModelPrice(2.50, 15.00),
    "gpt-5.4-mini": ModelPrice(0.75, 4.50),
    "gpt-5.4-nano": ModelPrice(0.20, 1.25),
    "gpt-5.4-pro": ModelPrice(30.00, 180.00),
}

_FREE = ModelPrice(0.0, 0.0)


def price_for_model(model: str) -> ModelPrice:
    """Model name -> $/1M pricing; unknown or local model names price at $0.

    Checks both provider tables by exact name (Anthropic and OpenAI model names never collide, so one
    merged lookup is unambiguous) — this function doesn't need to know which `llm_provider` is active,
    just the model string the caller already has. Unknown names (a future model not yet in either
    table, or a genuinely free local one) intentionally fall back to $0 rather than raising — an
    underestimate that shows up as "cheaper than reality" is far safer to ship than a KeyError taking
    down the /ask endpoint over a pricing lookup. See _OPENAI_PRICING's docstring: a $0 fallback here
    is exactly the silent-underestimate failure mode to watch for if you add a model and forget to
    price it.
    """
    return _ANTHROPIC_PRICING.get(model) or _OPENAI_PRICING.get(model) or _FREE


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    price = price_for_model(model)
    return (
        (input_tokens / 1_000_000) * price.input_per_million
        + (output_tokens / 1_000_000) * price.output_per_million
        + (cache_creation_input_tokens / 1_000_000) * price.cache_write_per_million
        + (cache_read_input_tokens / 1_000_000) * price.cache_read_per_million
    )
