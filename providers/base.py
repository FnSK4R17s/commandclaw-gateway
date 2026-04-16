"""Base provider adapter ABC."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator

import httpx

from schemas.common import Deployment


class ProviderError(Exception):
    def __init__(self, status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        super().__init__(f"Provider returned {status_code}: {body}")


class BaseLLMProvider(ABC):
    """Abstract base class for LLM provider adapters.

    Internal canonical format is OpenAI. All adapters transform to/from OpenAI format.
    """

    @abstractmethod
    def validate_environment(self, deployment: Deployment) -> None:
        """Check required env vars exist. Raise ProviderError if not."""

    @abstractmethod
    def get_complete_url(self, deployment: Deployment, endpoint: str = "chat/completions") -> str:
        """Return the full provider URL for the given endpoint."""

    @abstractmethod
    def get_headers(self, deployment: Deployment) -> dict[str, str]:
        """Return provider-specific headers including auth."""

    @abstractmethod
    def transform_request(
        self,
        request_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform OpenAI-format request to provider wire format."""

    @abstractmethod
    def transform_response(
        self,
        response_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform provider response to OpenAI format."""

    @abstractmethod
    def transform_stream_chunk(
        self,
        chunk_line: str,
        deployment: Deployment,
    ) -> dict[str, Any] | None:
        """Transform one SSE data line to OpenAI chunk format. Returns None for non-data lines."""

    def resolve_api_key(self, deployment: Deployment) -> str:
        """Resolve the API key from env var reference."""
        key_ref = deployment.api_key_env
        if key_ref.startswith("os.environ/"):
            env_var = key_ref[len("os.environ/"):]
            return os.environ.get(env_var, "")
        # Already resolved
        return key_ref

    async def send_request(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float = 30.0,
        stream: bool = False,
    ) -> httpx.Response:
        """Send HTTP request to provider. Override for providers with custom transport."""
        async with httpx.AsyncClient(timeout=timeout) as client:
            if stream:
                # For streaming, use a different approach — caller handles the stream
                response = await client.post(url, headers=headers, json=body)
                response.raise_for_status()
                return response
            else:
                response = await client.post(url, headers=headers, json=body)
                return response

    async def send_streaming_request(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float = 30.0,
    ) -> AsyncGenerator[str, None]:
        """Send streaming request, yield raw SSE lines."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=300.0)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code != 200:
                    body_bytes = await response.aread()
                    raise ProviderError(
                        status_code=response.status_code,
                        body={"error": body_bytes.decode("utf-8", errors="replace")},
                        headers=dict(response.headers),
                    )
                async for line in response.aiter_lines():
                    if line.strip():
                        yield line
