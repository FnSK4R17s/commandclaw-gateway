"""Redis async client singleton and key pattern conventions."""

from __future__ import annotations

import redis.asyncio as aioredis

from config import settings

# Redis key patterns (all dimensions, v1 uses subset):
#
# Rate limiting:
#   gateway:rl:key:{key_id}:rpm                    (v1)
#   gateway:rl:key:{key_id}:tpm                    (v1)
#   gateway:rl:key:{key_id}:model:{model}:rpm      (v1.1)
#   gateway:rl:key:{key_id}:model:{model}:tpm      (v1.1)
#   gateway:rl:user:{user_id}:rpm                  (v1.1)
#   gateway:rl:team:{team_id}:rpm                  (v1.1)
#   gateway:rl:team:{team_id}:model:{model}:rpm    (v1.1)
#   gateway:rl:org:{org_id}:rpm                    (v1.2)
#   gateway:rl:model:{model}:rpm                   (v1.1 global per-model)
#
# Spend tracking:
#   gateway:spend:key:{key_id}                     (v1)
#   gateway:spend:user:{user_id}                   (v1)
#   gateway:spend:team:{team_id}                   (v1.1)
#   gateway:spend:org:{org_id}                     (v1.2)
#
# Virtual keys:
#   gateway:vk:{key_id}                            (v1 — hash)
#   gateway:vk:hash:{sha256} -> key_id             (v1 — lookup index)
#
# Cache:
#   gateway:cache:{hash}                           (v1)
#   gateway:cache:team:{team_id}:{hash}            (v1.1)
#
# Spend logs:
#   gateway:spend_logs:{key_id}                    (v1 — list, capped 10k)
#   gateway:spend_logs:all                         (v1 — list, capped 10k)
#
# Cooldowns:
#   gateway:cooldown:failures:{deployment_id}      (v1 — counter)
#   gateway:cooldown:active:{deployment_id}        (v1 — flag with TTL)
#
# Concurrent requests:
#   gateway:busy:{deployment_id}                   (v1 — counter)

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            decode_responses=True,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
    return _redis


async def redis_health_check() -> bool:
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None
