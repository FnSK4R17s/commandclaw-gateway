"""Deployment cooldown tracking — pause failing deployments."""

from __future__ import annotations

import redis.asyncio as aioredis

FAILURE_KEY = "gateway:cooldown:failures:{}"
ACTIVE_KEY = "gateway:cooldown:active:{}"


async def increment_failure(deployment_id: str, redis: aioredis.Redis) -> int:
    key = FAILURE_KEY.format(deployment_id)
    count = await redis.incr(key)
    # Auto-expire failure counter after 5 minutes of no failures
    await redis.expire(key, 300)
    return count


async def maybe_cooldown(
    deployment_id: str,
    failure_count: int,
    allowed_fails: int,
    cooldown_time: int,
    redis: aioredis.Redis,
) -> bool:
    """Mark deployment as cooled down if failure threshold exceeded. Returns True if cooldown activated."""
    if failure_count >= allowed_fails:
        await redis.setex(ACTIVE_KEY.format(deployment_id), cooldown_time, "1")
        # Reset failure counter
        await redis.delete(FAILURE_KEY.format(deployment_id))
        return True
    return False


async def is_cooled_down(deployment_id: str, redis: aioredis.Redis) -> bool:
    return bool(await redis.exists(ACTIVE_KEY.format(deployment_id)))


async def reset_cooldown(deployment_id: str, redis: aioredis.Redis) -> None:
    """Reset on successful request — clear failure counter."""
    await redis.delete(FAILURE_KEY.format(deployment_id))
