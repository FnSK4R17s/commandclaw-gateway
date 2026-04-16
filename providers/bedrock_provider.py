"""AWS Bedrock provider adapter — uses boto3 for SigV4 signing."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncGenerator

import httpx

from providers.base import BaseLLMProvider
from schemas.common import Deployment


class BedrockProvider(BaseLLMProvider):
    """Bedrock adapter. Uses Bedrock Converse API for simplified transformations."""

    def validate_environment(self, deployment: Deployment) -> None:
        if not os.environ.get("AWS_ACCESS_KEY_ID") and not os.environ.get("AWS_PROFILE"):
            raise RuntimeError("AWS credentials not configured for Bedrock")

    def get_complete_url(self, deployment: Deployment, endpoint: str = "converse") -> str:
        region = deployment.region or os.environ.get("AWS_REGION", "us-east-1")
        return f"https://bedrock-runtime.{region}.amazonaws.com"

    def get_headers(self, deployment: Deployment) -> dict[str, str]:
        # boto3 handles auth — headers are set by the SDK
        return {"Content-Type": "application/json"}

    def transform_request(
        self,
        request_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform to Bedrock Converse API format."""
        messages = request_data.get("messages", [])
        converse_messages = []
        system_prompts = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                text = content if isinstance(content, str) else str(content)
                system_prompts.append({"text": text})
                continue

            converse_role = "assistant" if role == "assistant" else "user"
            if isinstance(content, str):
                converse_messages.append({
                    "role": converse_role,
                    "content": [{"text": content}],
                })
            elif isinstance(content, list):
                blocks = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        blocks.append({"text": block.get("text", "")})
                if blocks:
                    converse_messages.append({"role": converse_role, "content": blocks})

        data: dict[str, Any] = {
            "modelId": deployment.provider_model,
            "messages": converse_messages,
        }

        if system_prompts:
            data["system"] = system_prompts

        inference_config: dict[str, Any] = {}
        if request_data.get("max_tokens"):
            inference_config["maxTokens"] = request_data["max_tokens"]
        if request_data.get("temperature") is not None:
            inference_config["temperature"] = request_data["temperature"]
        if request_data.get("top_p") is not None:
            inference_config["topP"] = request_data["top_p"]
        if request_data.get("stop"):
            stops = request_data["stop"]
            inference_config["stopSequences"] = stops if isinstance(stops, list) else [stops]

        if inference_config:
            data["inferenceConfig"] = inference_config

        return data

    def transform_response(
        self,
        response_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform Bedrock Converse response to OpenAI format."""
        output = response_data.get("output", {})
        message = output.get("message", {})
        content_blocks = message.get("content", [])

        text_parts = [b.get("text", "") for b in content_blocks if "text" in b]
        content = "".join(text_parts) if text_parts else None

        reason_map = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
        }
        stop_reason = response_data.get("stopReason", "end_turn")

        usage = response_data.get("usage", {})

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": deployment.provider_model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": reason_map.get(stop_reason, "stop"),
            }],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
                "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
            },
        }

    def transform_stream_chunk(
        self,
        chunk_line: str,
        deployment: Deployment,
    ) -> dict[str, Any] | None:
        """Bedrock streaming uses event-stream format — handled by boto3 in send_streaming_request."""
        # This method is not used for Bedrock — boto3 handles event stream parsing
        return None

    async def send_request(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float = 30.0,
        stream: bool = False,
    ) -> httpx.Response:
        """Use boto3 for SigV4-signed requests instead of raw httpx."""
        import boto3

        region = body.pop("_region", os.environ.get("AWS_REGION", "us-east-1"))
        model_id = body.get("modelId", "")

        def _call():
            client = boto3.client("bedrock-runtime", region_name=region)
            if stream:
                response = client.converse_stream(
                    modelId=model_id,
                    messages=body.get("messages", []),
                    system=body.get("system", []),
                    inferenceConfig=body.get("inferenceConfig", {}),
                )
                # Collect streaming events into a single response for now
                # v2: proper event stream forwarding
                output_text = ""
                usage_data = {}
                stop_reason = "end_turn"
                for event in response.get("stream", []):
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {})
                        output_text += delta.get("text", "")
                    if "metadata" in event:
                        usage_data = event["metadata"].get("usage", {})
                    if "messageStop" in event:
                        stop_reason = event["messageStop"].get("stopReason", "end_turn")

                return {
                    "output": {"message": {"role": "assistant", "content": [{"text": output_text}]}},
                    "stopReason": stop_reason,
                    "usage": usage_data,
                }
            else:
                response = client.converse(
                    modelId=model_id,
                    messages=body.get("messages", []),
                    system=body.get("system", []),
                    inferenceConfig=body.get("inferenceConfig", {}),
                )
                return response

        result = await asyncio.to_thread(_call)

        # Wrap boto3 result into an httpx.Response-like object
        mock_response = httpx.Response(
            status_code=200,
            json=result,
            request=httpx.Request("POST", url),
        )
        return mock_response

    async def send_streaming_request(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float = 30.0,
    ) -> AsyncGenerator[str, None]:
        """Bedrock streaming via boto3 event stream."""
        import boto3

        region = os.environ.get("AWS_REGION", "us-east-1")
        model_id = body.get("modelId", "")

        def _stream():
            client = boto3.client("bedrock-runtime", region_name=region)
            response = client.converse_stream(
                modelId=model_id,
                messages=body.get("messages", []),
                system=body.get("system", []),
                inferenceConfig=body.get("inferenceConfig", {}),
            )
            events = []
            for event in response.get("stream", []):
                events.append(event)
            return events

        events = await asyncio.to_thread(_stream)

        stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # Emit as OpenAI-format SSE
        for event in events:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                text = delta.get("text", "")
                if text:
                    chunk = {
                        "id": stream_id, "object": "chat.completion.chunk",
                        "created": created, "model": model_id,
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}"
            if "messageStop" in event:
                stop = event["messageStop"].get("stopReason", "end_turn")
                finish_map = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}
                chunk = {
                    "id": stream_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_map.get(stop, "stop")}],
                }
                yield f"data: {json.dumps(chunk)}"
            if "metadata" in event:
                usage = event["metadata"].get("usage", {})
                if usage:
                    chunk = {
                        "id": stream_id, "object": "chat.completion.chunk",
                        "created": created, "model": model_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                        "usage": {
                            "prompt_tokens": usage.get("inputTokens", 0),
                            "completion_tokens": usage.get("outputTokens", 0),
                            "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
                        },
                    }
                    yield f"data: {json.dumps(chunk)}"

        yield "data: [DONE]"
