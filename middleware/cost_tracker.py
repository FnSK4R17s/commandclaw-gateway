"""Cost tracking — token extraction, spend accumulation, spend logs."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import ulid

from auth.budgets import increment_spend
from infra.cost_calculator import calculate_cost
from infra.redis_client import get_redis
from schemas.common import GatewayRequestContext, SpendLog

logger = logging.getLogger(__name__)

SPEND_LOG_CAP = 10_000


def extract_usage_from_response(response: dict[str, Any], provider: str = "openai") -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a response.

    Always reads the normalized OpenAI fields first (prompt_tokens/completion_tokens),
    since the gateway normalizes all provider responses to OpenAI format before this
    function is called in the main request paths. Falls back to Anthropic field names
    for cases where a raw Anthropic response is passed directly.
    """
    usage = response.get("usage", {})
    if not usage:
        return 0, 0

    # Try OpenAI-normalized fields first (this is what the gateway produces)
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)

    # Fall back to Anthropic-native fields if OpenAI fields are zero
    if prompt == 0 and completion == 0:
        prompt = usage.get("input_tokens", 0)
        completion = usage.get("output_tokens", 0)

    return prompt, completion


async def record_spend(
    context: GatewayRequestContext,
    input_tokens: int,
    output_tokens: int,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SpendLog:
    """Calculate cost, increment counters, store spend log."""
    cost = calculate_cost(context.model, input_tokens, output_tokens)
    if context.cache_hit:
        cost = 0.0

    # Increment spend counters at all hierarchy levels (atomic)
    if cost > 0 and context.identity:
        await increment_spend(
            context.identity.key_id,
            context.identity.user_id,
            cost,
            team_id=context.identity.team_id,
            org_id=context.identity.org_id,
        )

    # Build spend log
    log = SpendLog(
        log_id=str(ulid.ULID()),
        request_id=context.request_id,
        key_id=context.identity.key_id if context.identity else "",
        user_id=context.identity.user_id if context.identity else "",
        team_id=context.identity.team_id if context.identity else None,
        org_id=context.identity.org_id if context.identity else None,
        model=context.model,
        provider=context.provider or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        cache_hit=context.cache_hit,
        tags=tags or [],
        metadata=metadata or {},
        created_at=time.time(),
    )

    # Store in Redis lists
    redis = await get_redis()
    log_json = log.model_dump_json()
    pipe = redis.pipeline()
    if context.identity:
        pipe.lpush(f"gateway:spend_logs:{context.identity.key_id}", log_json)
        pipe.ltrim(f"gateway:spend_logs:{context.identity.key_id}", 0, SPEND_LOG_CAP - 1)
    pipe.lpush("gateway:spend_logs:all", log_json)
    pipe.ltrim("gateway:spend_logs:all", 0, SPEND_LOG_CAP - 1)
    await pipe.execute()

    # Update Prometheus metrics (lazy import to avoid circular dep)
    try:
        from observability.metrics import record_spend_metrics
        record_spend_metrics(context, input_tokens, output_tokens, cost)
    except ImportError:
        pass

    return log


async def get_spend_logs(
    key_id: str | None = None,
    user_id: str | None = None,
    tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query spend logs from Redis."""
    redis = await get_redis()

    if key_id:
        list_key = f"gateway:spend_logs:{key_id}"
    else:
        list_key = "gateway:spend_logs:all"

    raw_logs = await redis.lrange(list_key, offset, offset + limit - 1)
    logs = []
    for raw in raw_logs:
        try:
            log = json.loads(raw)
            if user_id and log.get("user_id") != user_id:
                continue
            if tag and tag not in log.get("tags", []):
                continue
            logs.append(log)
        except (json.JSONDecodeError, TypeError):
            continue

    return logs
