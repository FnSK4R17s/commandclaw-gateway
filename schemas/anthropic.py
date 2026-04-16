"""Anthropic Messages API wire format Pydantic models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Content blocks ──


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageSource(BaseModel):
    type: Literal["base64", "url"] = "base64"
    media_type: str = "image/png"
    data: str = ""
    url: str | None = None


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    source: ImageSource


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[TextBlock | ImageBlock] = ""
    is_error: bool = False


ContentBlock = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock


# ── Tool definitions ──


class AnthropicToolDef(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


# ── Request ──


class AnthropicMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]


class AnthropicRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    max_tokens: int
    system: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stream: bool = False
    tools: list[AnthropicToolDef] | None = None
    tool_choice: dict[str, Any] | None = None
    stop_sequences: list[str] | None = None
    metadata: dict[str, Any] | None = None


# ── Response ──


class AnthropicUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AnthropicResponse(BaseModel):
    id: str
    type: str = "message"
    role: str = "assistant"
    content: list[ContentBlock]
    model: str
    stop_reason: Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"] | None = None
    stop_sequence: str | None = None
    usage: AnthropicUsage


# ── Streaming events ──


class MessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"
    message: AnthropicResponse


class ContentBlockStartEvent(BaseModel):
    type: Literal["content_block_start"] = "content_block_start"
    index: int
    content_block: ContentBlock


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class InputJsonDelta(BaseModel):
    type: Literal["input_json_delta"] = "input_json_delta"
    partial_json: str


class ContentBlockDeltaEvent(BaseModel):
    type: Literal["content_block_delta"] = "content_block_delta"
    index: int
    delta: TextDelta | InputJsonDelta


class ContentBlockStopEvent(BaseModel):
    type: Literal["content_block_stop"] = "content_block_stop"
    index: int


class MessageDeltaUsage(BaseModel):
    output_tokens: int = 0


class MessageDelta(BaseModel):
    stop_reason: str | None = None
    stop_sequence: str | None = None


class MessageDeltaEvent(BaseModel):
    type: Literal["message_delta"] = "message_delta"
    delta: MessageDelta
    usage: MessageDeltaUsage


class MessageStopEvent(BaseModel):
    type: Literal["message_stop"] = "message_stop"


class PingEvent(BaseModel):
    type: Literal["ping"] = "ping"


# ── Count tokens ──


class AnthropicCountTokensRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    system: str | None = None
    tools: list[AnthropicToolDef] | None = None


class AnthropicCountTokensResponse(BaseModel):
    input_tokens: int
