"""Google Vertex AI provider adapter — Gemini generateContent format."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from providers.base import BaseLLMProvider
from schemas.common import Deployment


class VertexProvider(BaseLLMProvider):
    """Vertex AI adapter. Handles Google OAuth and Gemini wire format."""

    _credentials = None

    def validate_environment(self, deployment: Deployment) -> None:
        creds_path = deployment.api_key_env
        if creds_path.startswith("os.environ/"):
            env_var = creds_path[len("os.environ/"):]
            creds_path = os.environ.get(env_var, "")

        if not creds_path and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            raise RuntimeError("Google credentials not configured for Vertex AI")

    def _get_access_token(self, deployment: Deployment) -> str:
        import google.auth
        import google.auth.transport.requests

        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())

        return self._credentials.token

    def get_complete_url(self, deployment: Deployment, endpoint: str = "generateContent") -> str:
        region = deployment.region or "us-central1"
        model = deployment.provider_model
        # Extract project from env or model_info
        project = deployment.model_info.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        return (
            f"https://{region}-aiplatform.googleapis.com/v1/"
            f"projects/{project}/locations/{region}/"
            f"publishers/google/models/{model}:{endpoint}"
        )

    def get_headers(self, deployment: Deployment) -> dict[str, str]:
        token = self._get_access_token(deployment)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def transform_request(
        self,
        request_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform OpenAI messages to Gemini generateContent format."""
        messages = request_data.get("messages", [])
        contents = []
        system_instruction = None

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                system_instruction = content if isinstance(content, str) else str(content)
                continue

            gemini_role = "model" if role == "assistant" else "user"
            parts = []

            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append({"text": block.get("text", "")})
                        elif block.get("type") == "image_url":
                            url = block.get("image_url", {}).get("url", "")
                            parts.append({"fileData": {"fileUri": url}})

            if parts:
                contents.append({"role": gemini_role, "parts": parts})

        data: dict[str, Any] = {"contents": contents}

        if system_instruction:
            data["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        generation_config: dict[str, Any] = {}
        if request_data.get("temperature") is not None:
            generation_config["temperature"] = request_data["temperature"]
        if request_data.get("top_p") is not None:
            generation_config["topP"] = request_data["top_p"]
        if request_data.get("max_tokens") is not None:
            generation_config["maxOutputTokens"] = request_data["max_tokens"]
        if request_data.get("stop"):
            stops = request_data["stop"]
            generation_config["stopSequences"] = stops if isinstance(stops, list) else [stops]

        if generation_config:
            data["generationConfig"] = generation_config

        return data

    def transform_response(
        self,
        response_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform Gemini response to OpenAI format."""
        candidates = response_data.get("candidates", [])
        content_text = ""
        finish_reason = "stop"

        if candidates:
            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])
            texts = [p.get("text", "") for p in parts if "text" in p]
            content_text = "".join(texts)

            reason_map = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "stop"}
            finish_reason = reason_map.get(candidate.get("finishReason", "STOP"), "stop")

        usage_meta = response_data.get("usageMetadata", {})

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": deployment.provider_model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content_text or None},
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                "total_tokens": usage_meta.get("totalTokenCount", 0),
            },
        }

    def transform_stream_chunk(
        self,
        chunk_line: str,
        deployment: Deployment,
    ) -> dict[str, Any] | None:
        """Parse Vertex streaming response (JSON array lines)."""
        line = chunk_line.strip().rstrip(",")
        if line in ("", "[", "]"):
            return None
        if line.startswith("data: "):
            line = line[6:]

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        candidates = data.get("candidates", [])
        if not candidates:
            return None

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if "text" in p)

        finish_reason = None
        if candidate.get("finishReason"):
            reason_map = {"STOP": "stop", "MAX_TOKENS": "length"}
            finish_reason = reason_map.get(candidate["finishReason"], "stop")

        chunk: dict[str, Any] = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": deployment.provider_model,
            "choices": [{
                "index": 0,
                "delta": {"content": text} if text else {},
                "finish_reason": finish_reason,
            }],
        }

        usage_meta = data.get("usageMetadata", {})
        if usage_meta and finish_reason:
            chunk["usage"] = {
                "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                "total_tokens": usage_meta.get("totalTokenCount", 0),
            }

        if finish_reason:
            return chunk
        if text:
            return chunk
        return None
