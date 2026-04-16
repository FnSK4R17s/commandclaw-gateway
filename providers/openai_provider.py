"""OpenAI provider adapter — baseline for all OpenAI-compatible providers."""

from __future__ import annotations

import json
from typing import Any

from providers.base import BaseLLMProvider
from schemas.common import Deployment


class OpenAIProvider(BaseLLMProvider):
    """OpenAI adapter. Also serves as base for OpenAI-compatible providers (Groq, DeepSeek, Ollama)."""

    def validate_environment(self, deployment: Deployment) -> None:
        api_key = self.resolve_api_key(deployment)
        if not api_key:
            raise RuntimeError(f"API key not set for deployment {deployment.deployment_id}")

    def get_complete_url(self, deployment: Deployment, endpoint: str = "chat/completions") -> str:
        base = deployment.api_base.rstrip("/")
        if not base:
            base = "https://api.openai.com/v1"
        return f"{base}/{endpoint}"

    def get_headers(self, deployment: Deployment) -> dict[str, str]:
        api_key = self.resolve_api_key(deployment)
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def transform_request(
        self,
        request_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Near no-op — OpenAI is the canonical format. Just set the model."""
        data = dict(request_data)
        data["model"] = deployment.provider_model
        # Remove gateway-specific fields
        data.pop("metadata", None)
        return data

    def transform_response(
        self,
        response_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """No-op — response is already in OpenAI format."""
        return response_data

    def transform_stream_chunk(
        self,
        chunk_line: str,
        deployment: Deployment,
    ) -> dict[str, Any] | None:
        """Parse SSE data line to chunk dict."""
        line = chunk_line.strip()
        if not line.startswith("data: "):
            return None

        data_str = line[6:]  # Remove "data: " prefix
        if data_str == "[DONE]":
            return {"_done": True}

        try:
            return json.loads(data_str)
        except json.JSONDecodeError:
            return None
