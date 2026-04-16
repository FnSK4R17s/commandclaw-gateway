"""Ollama provider — OpenAI-compatible, just different base URL."""

from __future__ import annotations

from providers.openai_provider import OpenAIProvider
from schemas.common import Deployment


class OllamaProvider(OpenAIProvider):
    def validate_environment(self, deployment: Deployment) -> None:
        # Ollama doesn't require an API key
        pass

    def get_complete_url(self, deployment: Deployment, endpoint: str = "chat/completions") -> str:
        base = deployment.api_base.rstrip("/") if deployment.api_base else "http://localhost:11434"
        return f"{base}/v1/{endpoint}"

    def get_headers(self, deployment: Deployment) -> dict[str, str]:
        return {"Content-Type": "application/json"}
