"""Anthropic provider adapter — Messages API wire format."""

from __future__ import annotations

import json
import uuid
import time
from typing import Any

from providers.base import BaseLLMProvider
from schemas.common import Deployment


class AnthropicProvider(BaseLLMProvider):
    """Handles Anthropic's Messages API — content blocks, system-as-field, typed SSE events."""

    def validate_environment(self, deployment: Deployment) -> None:
        api_key = self.resolve_api_key(deployment)
        if not api_key:
            raise RuntimeError(f"Anthropic API key not set for deployment {deployment.deployment_id}")

    def get_complete_url(self, deployment: Deployment, endpoint: str = "messages") -> str:
        base = deployment.api_base.rstrip("/") if deployment.api_base else "https://api.anthropic.com"
        return f"{base}/v1/{endpoint}"

    def get_headers(self, deployment: Deployment) -> dict[str, str]:
        api_key = self.resolve_api_key(deployment)
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def transform_request(
        self,
        request_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform OpenAI-format request to Anthropic Messages API format."""
        messages = list(request_data.get("messages", []))
        system = None

        # Extract system message
        if messages and messages[0].get("role") == "system":
            sys_msg = messages.pop(0)
            content = sys_msg.get("content", "")
            system = content if isinstance(content, str) else str(content)

        # Convert messages
        anthropic_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Map tool role to user with tool_result content
            if role == "tool":
                anthropic_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": content if isinstance(content, str) else str(content),
                    }],
                })
                continue

            # Map assistant tool_calls to tool_use content blocks
            if role == "assistant" and msg.get("tool_calls"):
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": args,
                    })
                anthropic_messages.append({"role": "assistant", "content": blocks})
                continue

            anthropic_messages.append({
                "role": role,
                "content": content,
            })

        data: dict[str, Any] = {
            "model": deployment.provider_model,
            "messages": anthropic_messages,
            "max_tokens": request_data.get("max_tokens") or 4096,
        }

        if system:
            data["system"] = system
        if request_data.get("temperature") is not None:
            data["temperature"] = request_data["temperature"]
        if request_data.get("top_p") is not None:
            data["top_p"] = request_data["top_p"]
        if request_data.get("stop"):
            stops = request_data["stop"]
            data["stop_sequences"] = stops if isinstance(stops, list) else [stops]
        if request_data.get("stream"):
            data["stream"] = True

        # Convert tools
        if request_data.get("tools"):
            data["tools"] = []
            for tool in request_data["tools"]:
                func = tool.get("function", {})
                data["tools"].append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {}),
                })

        return data

    def transform_response(
        self,
        response_data: dict[str, Any],
        deployment: Deployment,
    ) -> dict[str, Any]:
        """Transform Anthropic response to OpenAI format."""
        content_blocks = response_data.get("content", [])
        text_parts = []
        tool_calls = []

        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        # Map stop_reason
        stop_reason = response_data.get("stop_reason", "end_turn")
        finish_reason_map = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
        }
        finish_reason = finish_reason_map.get(stop_reason, "stop")

        usage = response_data.get("usage", {})

        return {
            "id": response_data.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": response_data.get("model", deployment.provider_model),
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            },
        }

    def transform_stream_chunk(
        self,
        chunk_line: str,
        deployment: Deployment,
    ) -> dict[str, Any] | None:
        """Transform Anthropic SSE events to OpenAI chunk format.

        Anthropic sends typed events:
          event: message_start / content_block_start / content_block_delta /
                 content_block_stop / message_delta / message_stop / ping
        """
        line = chunk_line.strip()

        # Track event type (stateful — set by "event:" lines, consumed by "data:" lines)
        if line.startswith("event: "):
            self._current_event = line[7:].strip()
            return None

        if not line.startswith("data: "):
            return None

        data_str = line[6:]
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return None

        event_type = getattr(self, "_current_event", "")

        if event_type == "message_start":
            msg = data.get("message", {})
            self._stream_model = msg.get("model", deployment.provider_model)
            self._stream_id = msg.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
            return {
                "id": self._stream_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": self._stream_model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": None,
                }],
            }

        if event_type == "content_block_delta":
            delta = data.get("delta", {})
            delta_type = delta.get("type", "")
            stream_id = getattr(self, "_stream_id", "chatcmpl-stream")
            stream_model = getattr(self, "_stream_model", deployment.provider_model)

            if delta_type == "text_delta":
                return {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": stream_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": delta.get("text", "")},
                        "finish_reason": None,
                    }],
                }
            elif delta_type == "input_json_delta":
                # Stream tool-call arguments as OpenAI tool_calls delta
                partial_json = delta.get("partial_json", "")
                block_index = data.get("index", 0)
                return {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": stream_model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": block_index,
                                "function": {"arguments": partial_json},
                            }],
                        },
                        "finish_reason": None,
                    }],
                }

        if event_type == "content_block_start":
            block = data.get("content_block", {})
            if block.get("type") == "tool_use":
                stream_id = getattr(self, "_stream_id", "chatcmpl-stream")
                stream_model = getattr(self, "_stream_model", deployment.provider_model)
                return {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": stream_model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": data.get("index", 0),
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": "",
                                },
                            }],
                        },
                        "finish_reason": None,
                    }],
                }

        if event_type == "message_delta":
            delta = data.get("delta", {})
            stop_reason = delta.get("stop_reason", "end_turn")
            finish_map = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}
            stream_id = getattr(self, "_stream_id", "chatcmpl-stream")
            stream_model = getattr(self, "_stream_model", deployment.provider_model)

            usage = data.get("usage", {})
            chunk: dict[str, Any] = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": stream_model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_map.get(stop_reason, "stop"),
                }],
            }
            if usage:
                chunk["usage"] = {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                }
            return chunk

        if event_type == "message_stop":
            return {"_done": True}

        return None
