from app.llm.pricing import estimate_cost_usd, price_for_model


def test_known_anthropic_model_priced_correctly():
    price = price_for_model("claude-opus-4-8")
    assert price.input_per_million == 5.00
    assert price.output_per_million == 25.00


def test_estimate_cost_matches_hand_computed_value():
    # 1M input tokens + 1M output tokens on Haiku ($1/$5 per 1M) = $1 + $5 = $6
    cost = estimate_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == 6.0


def test_unknown_or_local_model_prices_at_zero():
    cost = estimate_cost_usd("openai/gpt-oss-20b", input_tokens=500_000, output_tokens=500_000)
    assert cost == 0.0


def test_known_openai_model_priced_correctly():
    # Regression guard for the gap found live: switching AI_LLM_PROVIDER=openai_compatible from a
    # local LM Studio model to a real OpenAI one used to silently price at $0 (fell through to the
    # Anthropic-only table) — real spend would happen with the UI still showing $0.00.
    price = price_for_model("gpt-5.4")
    assert price.input_per_million == 2.50
    assert price.output_per_million == 15.00

    cost = estimate_cost_usd("gpt-5.4-mini", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == 0.75 + 4.50


def test_zero_tokens_costs_nothing():
    assert estimate_cost_usd("claude-opus-4-8", 0, 0) == 0.0


def test_cache_write_is_1_25x_base_input_price():
    price = price_for_model("claude-opus-4-8")  # $5/1M input
    assert price.cache_write_per_million == 6.25


def test_cache_read_is_one_tenth_base_input_price():
    price = price_for_model("claude-opus-4-8")  # $5/1M input
    assert price.cache_read_per_million == 0.5


def test_cache_tokens_priced_separately_from_plain_input():
    # 1M plain input + 1M cache-write + 1M cache-read, on Opus ($5/1M input):
    # 5.00 (plain) + 6.25 (write, 1.25x) + 0.50 (read, 0.1x) = 11.75
    cost = estimate_cost_usd(
        "claude-opus-4-8", input_tokens=1_000_000, output_tokens=0,
        cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000,
    )
    assert cost == 11.75


def test_a_cache_hit_is_much_cheaper_than_the_same_tokens_as_plain_input():
    # The entire point of caching: reading 1M cached tokens should cost far less than treating them
    # as 1M ordinary input tokens would.
    as_plain_input = estimate_cost_usd("claude-opus-4-8", input_tokens=1_000_000, output_tokens=0)
    as_cache_read = estimate_cost_usd(
        "claude-opus-4-8", input_tokens=0, output_tokens=0, cache_read_input_tokens=1_000_000
    )
    assert as_cache_read < as_plain_input * 0.15
