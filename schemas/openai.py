"""OpenAI wire format Pydantic models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from schemas.common import UsageBlock


# ── Request models ──


class ToolFunction(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class ToolCallFunction(BaseModel):
    name: str
    arguments: str  # JSON string


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class OpenAIMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict[str, Any] | None = None


class OpenAIChatRequest(BaseModel):
    model: str
    messages: list[OpenAIMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    tools: list[Tool] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: ResponseFormat | None = None
    seed: int | None = None
    stop: str | list[str] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None

    # Fields included in cache key hash
    CACHE_KEY_FIELDS: frozenset[str] = frozenset({
        "model", "messages", "temperature", "top_p", "max_tokens",
        "tools", "tool_choice", "response_format", "seed", "stop",
        "frequency_penalty", "presence_penalty",
    })

    model_config = {"populate_by_name": True}


# ── Response models ──


class OpenAIResponseMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class OpenAIChoice(BaseModel):
    index: int = 0
    message: OpenAIResponseMessage
    finish_reason: str | None = None


class OpenAIChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[OpenAIChoice]
    usage: UsageBlock | None = None
    system_fingerprint: str | None = None


# ── Streaming models ──


class OpenAIDelta(BaseModel):
    role: str | None = None
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class OpenAIStreamChoice(BaseModel):
    index: int = 0
    delta: OpenAIDelta
    finish_reason: str | None = None


class OpenAIStreamChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[OpenAIStreamChoice]
    usage: UsageBlock | None = None
    system_fingerprint: str | None = None


# ── Embeddings ──


class OpenAIEmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: Literal["float", "base64"] | None = None
    dimensions: int | None = None
    user: str | None = None


class EmbeddingObject(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class OpenAIEmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingObject]
    model: str
    usage: UsageBlock


# ── Models list ──


class OpenAIModel(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "commandclaw-gateway"


class OpenAIModelList(BaseModel):
    object: str = "list"
    data: list[OpenAIModel]
