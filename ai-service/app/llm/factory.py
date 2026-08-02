from __future__ import annotations

from app.config import Settings
from app.llm.anthropic_client import AnthropicClient
from app.llm.openai_compatible_client import OpenAICompatibleClient
from app.llm.types import LLMClient


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings.anthropic_api_key, settings.agent_model, settings.max_output_tokens)
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleClient(
            settings.openai_base_url,
            settings.openai_api_key,
            settings.agent_model,
            settings.max_output_tokens,
            settings.llm_request_timeout_seconds,
        )
    raise ValueError(f"unknown llm_provider: {settings.llm_provider}")
