"""Redis-backed rate limiter — multi-dimensional sliding window + token bucket (v2).

v1: per-key RPM/TPM
v1.1: per-key-per-model, per-user, per-team, per-org, per-model-global, daily/monthly quotas,
      upstream provider awareness, dynamic adjustment
v2: token bucket (burst), priority tiers
"""

from __future__ import annotations

import logging
import time

import redis.asyncio as aioredis

from schemas.common import IdentityContext

logger = logging.getLogger(__name__)

_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)
local count = redis.call('ZCARD', key)

if count >= limit then
    return {0, count, limit}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms)
return {1, count + 1, limit}
"""

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1]) or capacity
local last_refill = tonumber(data[2]) or now

local elapsed = now - last_refill
local new_tokens = elapsed * refill_rate
tokens = math.min(capacity, tokens + new_tokens)

if tokens >= requested then
    tokens = tokens - requested
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'last_refill', tostring(now))
    redis.call('EXPIRE', key, 120)
    return {1, tostring(tokens)}
end

redis.call('HMSET', key, 'tokens', tostring(tokens), 'last_refill', tostring(now))
redis.call('EXPIRE', key, 120)
return {0, tostring(tokens)}
"""


async def _check_sliding_window(
    redis: aioredis.Redis, key: str, limit: int, window_ms: int, now_ms: int, member: str,
) -> tuple[bool, int, int]:
    """Returns (allowed, current, limit)."""
    result = await redis.eval(_SLIDING_WINDOW_LUA, 1, key, str(window_ms), str(limit), str(now_ms), member)
    return bool(int(result[0])), int(result[1]), int(result[2])


async def _check_tpm(
    redis: aioredis.Redis, key: str, limit: int, tokens: int, now_ms: int, window_ms: int,
) -> tuple[bool, int]:
    """Returns (allowed, current_total)."""
    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, 0, now_ms - window_ms)
    pipe.zrangebyscore(key, now_ms - window_ms, "+inf", withscores=False)
    results = await pipe.execute()

    current_total = 0
    for member in results[1]:
        parts = str(member).rsplit(":", 1)
        if len(parts) == 2:
            try:
                current_total += int(parts[1])
            except ValueError:
                pass

    if current_total + tokens > limit:
        return False, current_total

    member = f"{now_ms}:{tokens}"
    await redis.zadd(key, {member: now_ms})
    await redis.pexpire(key, window_ms)
    return True, current_total + tokens


async def _check_quota(
    redis: aioredis.Redis, key: str, limit: int, increment: int = 1,
) -> tuple[bool, int]:
    """Check daily/monthly quota. Returns (allowed, current)."""
    current = int(await redis.get(key) or "0")
    if current + increment > limit:
        return False, current
    await redis.incrby(key, increment)
    return True, current + increment


async def check_and_increment_rate_limit(
    identity: IdentityContext,
    model: str,
    request_tokens: int,
    request_id: str,
    redis: aioredis.Redis,
    cache_hit: bool = False,
    use_token_bucket: bool = False,
) -> tuple[bool, dict[str, str]]:
    """Full multi-dimensional rate limit check."""
    now_ms = int(time.time() * 1000)
    window_ms = 60_000
    headers: dict[str, str] = {}

    # Per-key RPM
    if identity.rpm_limit:
        if use_token_bucket:
            # v2: token bucket for burst allowance
            tb_key = f"gateway:rl:tb:key:{identity.key_id}:rpm"
            allowed_val, remaining = await _token_bucket_check(
                redis, tb_key, identity.rpm_limit, identity.rpm_limit / 60.0, 1)
            headers["x-ratelimit-limit-requests"] = str(identity.rpm_limit)
            headers["x-ratelimit-remaining-requests"] = str(int(remaining))
            if not allowed_val:
                headers["retry-after"] = "1"
                _log_rate_limit_hit(identity, model, "key_rpm")
                return False, headers
        else:
            rpm_key = f"gateway:rl:key:{identity.key_id}:rpm"
            allowed, current, limit = await _check_sliding_window(
                redis, rpm_key, identity.rpm_limit, window_ms, now_ms, request_id)
            headers["x-ratelimit-limit-requests"] = str(limit)
            headers["x-ratelimit-remaining-requests"] = str(max(0, limit - current))
            if not allowed:
                headers["retry-after"] = "60"
                _log_rate_limit_hit(identity, model, "key_rpm")
                return False, headers

    # Per-key TPM (skip on cache hit)
    if identity.tpm_limit and not cache_hit and request_tokens > 0:
        tpm_key = f"gateway:rl:key:{identity.key_id}:tpm"
        tpm_ok, current_tpm = await _check_tpm(redis, tpm_key, identity.tpm_limit, request_tokens, now_ms, window_ms)
        headers["x-ratelimit-limit-tokens"] = str(identity.tpm_limit)
        headers["x-ratelimit-remaining-tokens"] = str(max(0, identity.tpm_limit - current_tpm))
        if not tpm_ok:
            headers["retry-after"] = "60"
            _log_rate_limit_hit(identity, model, "key_tpm")
            return False, headers

    # Per-key-per-model RPM/TPM (v1.1)
    per_model_limits = identity.metadata.get("per_model_limits", {}).get(model, {})
    if per_model_limits:
        model_rpm = per_model_limits.get("rpm")
        if model_rpm:
            km_key = f"gateway:rl:key:{identity.key_id}:model:{model}:rpm"
            allowed, _, _ = await _check_sliding_window(redis, km_key, model_rpm, window_ms, now_ms, request_id)
            if not allowed:
                headers["retry-after"] = "60"
                _log_rate_limit_hit(identity, model, "key_model_rpm")
                return False, headers

        model_tpm = per_model_limits.get("tpm")
        if model_tpm and not cache_hit and request_tokens > 0:
            km_tpm_key = f"gateway:rl:key:{identity.key_id}:model:{model}:tpm"
            tpm_ok, _ = await _check_tpm(redis, km_tpm_key, model_tpm, request_tokens, now_ms, window_ms)
            if not tpm_ok:
                headers["retry-after"] = "60"
                _log_rate_limit_hit(identity, model, "key_model_tpm")
                return False, headers

    # Per-user RPM (v1.1)
    user_rpm = identity.metadata.get("user_rpm_limit")
    if user_rpm:
        u_key = f"gateway:rl:user:{identity.user_id}:rpm"
        allowed, _, _ = await _check_sliding_window(redis, u_key, user_rpm, window_ms, now_ms, request_id)
        if not allowed:
            headers["retry-after"] = "60"
            _log_rate_limit_hit(identity, model, "user_rpm")
            return False, headers

    # Per-team RPM/TPM (v1.1)
    if identity.team_id:
        team_data = await redis.hgetall(f"gateway:team:{identity.team_id}")
        team_rpm = int(team_data.get("rpm_limit", "0")) if team_data else 0
        if team_rpm:
            t_key = f"gateway:rl:team:{identity.team_id}:rpm"
            allowed, _, _ = await _check_sliding_window(redis, t_key, team_rpm, window_ms, now_ms, request_id)
            if not allowed:
                headers["retry-after"] = "60"
                _log_rate_limit_hit(identity, model, "team_rpm")
                return False, headers

        team_tpm = int(team_data.get("tpm_limit", "0")) if team_data else 0
        if team_tpm and not cache_hit and request_tokens > 0:
            t_tpm_key = f"gateway:rl:team:{identity.team_id}:tpm"
            tpm_ok, _ = await _check_tpm(redis, t_tpm_key, team_tpm, request_tokens, now_ms, window_ms)
            if not tpm_ok:
                headers["retry-after"] = "60"
                _log_rate_limit_hit(identity, model, "team_tpm")
                return False, headers

    # Per-org RPM (v1.2)
    if identity.org_id:
        org_data = await redis.hgetall(f"gateway:org:{identity.org_id}")
        org_rpm = int(org_data.get("rpm_limit", "0")) if org_data else 0
        if org_rpm:
            o_key = f"gateway:rl:org:{identity.org_id}:rpm"
            allowed, _, _ = await _check_sliding_window(redis, o_key, org_rpm, window_ms, now_ms, request_id)
            if not allowed:
                headers["retry-after"] = "60"
                _log_rate_limit_hit(identity, model, "org_rpm")
                return False, headers

    # Per-model global RPM (v1.1)
    global_model_rpm = identity.metadata.get("global_model_rpm", {}).get(model)
    if global_model_rpm:
        gm_key = f"gateway:rl:model:{model}:rpm"
        allowed, _, _ = await _check_sliding_window(redis, gm_key, global_model_rpm, window_ms, now_ms, request_id)
        if not allowed:
            headers["retry-after"] = "60"
            _log_rate_limit_hit(identity, model, "model_global_rpm")
            return False, headers

    # Daily/monthly quotas (v1.1)
    daily_limit = identity.metadata.get("daily_token_quota")
    if daily_limit and not cache_hit:
        today = time.strftime("%Y%m%d")
        dq_key = f"gateway:quota:key:{identity.key_id}:daily:{today}"
        ok, _ = await _check_quota(redis, dq_key, daily_limit, request_tokens)
        if not ok:
            await redis.expire(dq_key, 86400)
            headers["retry-after"] = "3600"
            _log_rate_limit_hit(identity, model, "daily_quota")
            return False, headers
        await redis.expire(dq_key, 86400)

    monthly_limit = identity.metadata.get("monthly_token_quota")
    if monthly_limit and not cache_hit:
        month = time.strftime("%Y%m")
        mq_key = f"gateway:quota:key:{identity.key_id}:monthly:{month}"
        ok, _ = await _check_quota(redis, mq_key, monthly_limit, request_tokens)
        if not ok:
            await redis.expire(mq_key, 2592000)
            headers["retry-after"] = "86400"
            _log_rate_limit_hit(identity, model, "monthly_quota")
            return False, headers
        await redis.expire(mq_key, 2592000)

    return True, headers


async def _token_bucket_check(
    redis: aioredis.Redis, key: str, capacity: int, refill_rate: float, requested: int,
) -> tuple[bool, float]:
    now = time.time()
    result = await redis.eval(_TOKEN_BUCKET_LUA, 1, key, str(capacity), str(refill_rate), str(now), str(requested))
    return bool(int(result[0])), float(result[1])


async def update_upstream_limits(
    deployment_id: str,
    provider_headers: dict[str, str],
    redis: aioredis.Redis,
) -> None:
    """Read provider x-ratelimit-remaining-* headers and store for routing awareness (v1.1)."""
    remaining = provider_headers.get("x-ratelimit-remaining-requests")
    if remaining is not None:
        await redis.setex(f"gateway:upstream:remaining:{deployment_id}", 120, remaining)


async def get_upstream_remaining(deployment_id: str, redis: aioredis.Redis) -> int | None:
    """Get cached upstream remaining rate limit for a deployment."""
    val = await redis.get(f"gateway:upstream:remaining:{deployment_id}")
    return int(val) if val else None


def _log_rate_limit_hit(identity: IdentityContext, model: str, limit_type: str) -> None:
    """Log and record metrics for rate limit hits (v1.1)."""
    try:
        from observability.metrics import record_rate_limit_hit
        record_rate_limit_hit(identity.key_id, identity.team_id or "", model, limit_type)
    except ImportError:
        pass
    logger.info("Rate limit hit: key=%s model=%s type=%s", identity.key_id, model, limit_type)
