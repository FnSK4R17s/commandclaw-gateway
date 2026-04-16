"""Redis exact-match cache with streaming assembly and per-model TTL."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis

from config import get_config

logger = logging.getLogger(__name__)

# Fields that determine cache key — must include everything that affects output
CACHE_KEY_FIELDS = frozenset({
    "model", "messages", "temperature", "top_p", "max_tokens",
    "tools", "tool_choice", "response_format", "seed", "stop",
    "frequency_penalty", "presence_penalty",
})


def build_cache_key(
    request_data: dict[str, Any],
    namespace: str | None = None,
    cache_scope: str = "global",
    team_id: str | None = None,
    key_id: str | None = None,
) -> str:
    """Build a deterministic cache key with multi-tenant isolation (v1.1)."""
    relevant = {k: v for k, v in request_data.items() if k in CACHE_KEY_FIELDS and v is not None}
    canonical = json.dumps(relevant, sort_keys=True, default=str)
    hash_val = hashlib.sha256(canonical.encode()).hexdigest()

    if namespace:
        return f"gateway:cache:{namespace}:{hash_val}"
    if cache_scope == "team" and team_id:
        return f"gateway:cache:team:{team_id}:{hash_val}"
    if cache_scope == "key" and key_id:
        return f"gateway:cache:key:{key_id}:{hash_val}"
    return f"gateway:cache:{hash_val}"


def should_cache_call_type(call_type: str) -> bool:
    """Check if this call type should be cached based on config (v1)."""
    config = get_config()
    return call_type in config.cache_settings.supported_call_types


def _get_ttl_for_model(model: str) -> int:
    config = get_config()
    per_model = config.cache_settings.per_model_ttl
    if model in per_model:
        return per_model[model]
    # Try prefix match
    for name, ttl in per_model.items():
        if model.startswith(name):
            return ttl
    return config.cache_settings.default_ttl


def parse_cache_control(request_data: dict[str, Any]) -> dict[str, Any]:
    """Extract cache control directives from request metadata."""
    metadata = request_data.get("metadata", {}) or {}
    return {
        "no_cache": metadata.get("cache_control") == "no-cache",
        "no_store": metadata.get("cache_control") == "no-store",
        "ttl": metadata.get("cache_ttl"),
        "s_maxage": metadata.get("cache_s_maxage"),
        "namespace": metadata.get("cache_namespace"),
    }


async def get_cached_response(
    cache_key: str,
    redis: aioredis.Redis | None,
    s_maxage: int | None = None,
) -> dict[str, Any] | None:
    """Get a cached response. Supports Redis and in-memory backends."""
    config = get_config()

    if config.cache_settings.type == "none":
        return None

    try:
        if config.cache_settings.type == "memory":
            from middleware.memory_cache import get_memory_cache
            data = await get_memory_cache().get(cache_key)
        else:
            if redis is None:
                return None
            raw = await redis.get(cache_key)
            if raw is None:
                return None
            data = json.loads(raw)

        if data is None:
            return None

        if s_maxage is not None:
            cached_at = data.get("_cached_at", 0)
            if time.time() - cached_at > s_maxage:
                return None

        return data
    except Exception:
        logger.exception("Cache get error for key %s", cache_key)
        return None


async def set_cached_response(
    cache_key: str,
    response: dict[str, Any],
    model: str,
    redis: aioredis.Redis | None,
    ttl_override: int | None = None,
) -> None:
    """Write a response to cache. Supports Redis and in-memory backends."""
    config = get_config()
    if config.cache_settings.type == "none":
        return

    try:
        response["_cached_at"] = time.time()
        response["model"] = model  # Store model for targeted invalidation
        ttl = ttl_override or _get_ttl_for_model(model)

        if config.cache_settings.type == "memory":
            from middleware.memory_cache import get_memory_cache
            await get_memory_cache().set(cache_key, response, ttl)
        else:
            if redis is None:
                return
            await redis.setex(cache_key, ttl, json.dumps(response, default=str))
            # Secondary index: model -> cache keys (for targeted invalidation)
            await redis.sadd(f"gateway:cache_index:model:{model}", cache_key)
            await redis.expire(f"gateway:cache_index:model:{model}", ttl + 60)
    except Exception:
        logger.exception("Cache set error for key %s", cache_key)


async def delete_cache(
    redis: aioredis.Redis,
    model: str | None = None,
    cache_key: str | None = None,
) -> int:
    """Delete cached responses. Returns count of deleted keys."""
    if cache_key:
        return await redis.delete(cache_key)

    if model:
        # Use secondary index for O(1) targeted invalidation (v1.1)
        index_key = f"gateway:cache_index:model:{model}"
        cache_keys = await redis.smembers(index_key)
        deleted = 0
        if cache_keys:
            deleted = await redis.delete(*cache_keys)
            await redis.delete(index_key)
        return deleted

    # Flush all cache
    cursor = 0
    deleted = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="gateway:cache:*", count=200)
        if keys:
            deleted += await redis.delete(*keys)
        if cursor == 0:
            break
    return deleted


class StreamingCacheAssembler:
    """Buffers streaming chunks for cache while forwarding to client.

    Usage:
        assembler = StreamingCacheAssembler(cache_key, model, redis)
        async for chunk in provider_stream:
            assembler.add_chunk(chunk)
            yield chunk
        await assembler.finalize()  # writes to cache only on success
    """

    def __init__(self, cache_key: str, model: str, redis: aioredis.Redis, no_store: bool = False):
        self.cache_key = cache_key
        self.model = model
        self.redis = redis
        self.no_store = no_store
        self.chunks: list[dict[str, Any]] = []
        self.full_content = ""
        self.tool_calls: list[dict] = []
        self.usage: dict[str, int] | None = None
        self.response_id = ""
        self.finish_reason: str | None = None
        self.completed = False

    def add_chunk(self, chunk: dict[str, Any]) -> None:
        self.chunks.append(chunk)

        if not self.response_id and chunk.get("id"):
            self.response_id = chunk["id"]

        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            if delta.get("content"):
                self.full_content += delta["content"]
            if delta.get("tool_calls"):
                self.tool_calls.extend(delta["tool_calls"])
            if choices[0].get("finish_reason"):
                self.finish_reason = choices[0]["finish_reason"]

        if chunk.get("usage"):
            self.usage = chunk["usage"]

    async def finalize(self) -> None:
        """Write assembled response to cache. Call only after successful stream completion."""
        if self.no_store:
            return
        self.completed = True

        # Reconstruct a non-streaming response for cache storage
        assembled = {
            "id": self.response_id,
            "object": "chat.completion",
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": self.full_content or None,
                    "tool_calls": self.tool_calls or None,
                },
                "finish_reason": self.finish_reason,
            }],
            "usage": self.usage,
        }
        await set_cached_response(self.cache_key, assembled, self.model, self.redis)


async def rechunk_for_streaming(
    cached_response: dict[str, Any],
    response_format: str = "openai",
) -> AsyncGenerator[str, None]:
    """Re-emit a cached non-streaming response as SSE chunks.

    Used when a streaming request hits the cache.
    """
    import time as _time

    resp_id = cached_response.get("id", "chatcmpl-cached")
    model = cached_response.get("model", "unknown")
    created = int(_time.time())
    choices = cached_response.get("choices", [])

    if not choices:
        return

    message = choices[0].get("message", {})
    content = message.get("content", "")

    if response_format == "openai":
        # Initial chunk with role
        yield f"data: {json.dumps({'id': resp_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"

        # Content chunks (simulate ~20 char chunks)
        if content:
            chunk_size = 20
            for i in range(0, len(content), chunk_size):
                text_chunk = content[i:i + chunk_size]
                yield f"data: {json.dumps({'id': resp_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'content': text_chunk}, 'finish_reason': None}]})}\n\n"

        # Tool calls
        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                yield f"data: {json.dumps({'id': resp_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'tool_calls': [tc]}, 'finish_reason': None}]})}\n\n"

        # Final chunk
        finish_reason = choices[0].get("finish_reason", "stop")
        yield f"data: {json.dumps({'id': resp_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish_reason}]})}\n\n"
        yield "data: [DONE]\n\n"

    elif response_format == "anthropic":
        # Re-emit as Anthropic typed events
        usage = cached_response.get("usage", {})
        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': resp_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model, 'stop_reason': None, 'usage': {'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': 0}}})}\n\n"

        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

        if content:
            chunk_size = 20
            for i in range(0, len(content), chunk_size):
                text_chunk = content[i:i + chunk_size]
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text_chunk}})}\n\n"

        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

        stop_reason = choices[0].get("finish_reason", "stop")
        anthropic_stop = {"stop": "end_turn", "length": "max_tokens"}.get(stop_reason, "end_turn")
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': anthropic_stop}, 'usage': {'output_tokens': usage.get('completion_tokens', 0)}})}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
