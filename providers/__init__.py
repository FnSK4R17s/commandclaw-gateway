"""Provider registry."""

from __future__ import annotations

from providers.anthropic_provider import AnthropicProvider
from providers.base import BaseLLMProvider
from providers.bedrock_provider import BedrockProvider
from providers.ollama_provider import OllamaProvider
from providers.openai_provider import OpenAIProvider
from providers.vertex_provider import VertexProvider

PROVIDER_REGISTRY: dict[str, type[BaseLLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "vertex": VertexProvider,
    "bedrock": BedrockProvider,
    "ollama": OllamaProvider,
    # OpenAI-compatible providers use the OpenAI adapter
    "groq": OpenAIProvider,
    "deepseek": OpenAIProvider,
}

_instances: dict[str, BaseLLMProvider] = {}


def get_provider(provider_name: str) -> BaseLLMProvider:
    if provider_name not in _instances:
        cls = PROVIDER_REGISTRY.get(provider_name)
        if not cls:
            raise ValueError(f"Unknown provider: {provider_name}")
        _instances[provider_name] = cls()
    return _instances[provider_name]
