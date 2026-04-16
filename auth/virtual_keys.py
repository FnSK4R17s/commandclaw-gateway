"""Virtual key lifecycle: generate, validate, block, rotate, delete."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

import ulid

from infra.redis_client import get_redis
from schemas.common import IdentityContext

KEY_PREFIX = "sk-cc-"
VK_HASH_PREFIX = "gateway:vk:hash:"
VK_PREFIX = "gateway:vk:"


def _generate_raw_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def generate_key(
    user_id: str,
    models: list[str] | None = None,
    max_budget: float | None = None,
    budget_duration: str | None = None,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    max_parallel_requests: int | None = None,
    team_id: str | None = None,
    org_id: str | None = None,
    key_alias: str | None = None,
    guardrail_policy: str | None = None,
    metadata: dict[str, Any] | None = None,
    expires_in: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate a new virtual key. Returns (raw_key, key_info)."""
    redis = await get_redis()
    raw_key = _generate_raw_key()
    key_hash = _hash_key(raw_key)
    key_id = str(ulid.ULID())
    now = time.time()

    budget_reset_at = ""
    if budget_duration and max_budget:
        if budget_duration == "daily":
            budget_reset_at = str(now + 86400)
        elif budget_duration == "monthly":
            budget_reset_at = str(now + 2592000)

    expires_at = str(now + expires_in) if expires_in else ""

    key_data = {
        "key_id": key_id,
        "key_hash": key_hash,
        "user_id": user_id,
        "team_id": team_id or "",
        "org_id": org_id or "",
        "user_role": "internal_user",
        "key_alias": key_alias or "",
        "model_allowlist": json.dumps(models) if models else "",
        "max_budget": str(max_budget) if max_budget is not None else "",
        "budget_duration": budget_duration or "",
        "budget_reset_at": budget_reset_at,
        "rpm_limit": str(rpm_limit) if rpm_limit is not None else "",
        "tpm_limit": str(tpm_limit) if tpm_limit is not None else "",
        "max_parallel_requests": str(max_parallel_requests) if max_parallel_requests is not None else "",
        "guardrail_policy": guardrail_policy or "",
        "is_blocked": "0",
        "created_at": str(now),
        "expires_at": expires_at,
        "metadata": json.dumps(metadata or {}),
    }

    pipe = redis.pipeline()
    pipe.hset(f"{VK_PREFIX}{key_id}", mapping=key_data)
    pipe.set(f"{VK_HASH_PREFIX}{key_hash}", key_id)
    if expires_at:
        ttl = int(float(expires_at) - now)
        pipe.expire(f"{VK_PREFIX}{key_id}", ttl)
        pipe.expire(f"{VK_HASH_PREFIX}{key_hash}", ttl)
    await pipe.execute()

    return raw_key, {**key_data, "key_id": key_id}


async def validate_key(raw_key: str) -> IdentityContext | None:
    """Validate a raw key and return IdentityContext. Returns None if invalid."""
    redis = await get_redis()
    key_hash = _hash_key(raw_key)

    key_id = await redis.get(f"{VK_HASH_PREFIX}{key_hash}")
    if not key_id:
        return None

    data = await redis.hgetall(f"{VK_PREFIX}{key_id}")
    if not data:
        return None

    if data.get("is_blocked") == "1":
        return None

    expires_at = data.get("expires_at", "")
    if expires_at and float(expires_at) < time.time():
        return None

    # Load current spend for budget_remaining
    spend = float(await redis.get(f"gateway:spend:key:{key_id}") or "0")
    max_budget = float(data["max_budget"]) if data.get("max_budget") else None
    budget_remaining = (max_budget - spend) if max_budget is not None else float("inf")

    model_allowlist = json.loads(data["model_allowlist"]) if data.get("model_allowlist") else None

    return IdentityContext(
        key_id=key_id,
        user_id=data.get("user_id", ""),
        team_id=data.get("team_id") or None,
        org_id=data.get("org_id") or None,
        key_alias=data.get("key_alias") or None,
        user_role=data.get("user_role", "internal_user"),
        model_allowlist=model_allowlist,
        max_budget=max_budget,
        budget_duration=data.get("budget_duration") or None,
        budget_remaining=budget_remaining,
        rpm_limit=int(data["rpm_limit"]) if data.get("rpm_limit") else None,
        tpm_limit=int(data["tpm_limit"]) if data.get("tpm_limit") else None,
        max_parallel_requests=int(data["max_parallel_requests"]) if data.get("max_parallel_requests") else None,
        guardrail_policy=data.get("guardrail_policy") or None,
        metadata=json.loads(data.get("metadata", "{}")),
    )


async def block_key(key_id: str) -> bool:
    redis = await get_redis()
    return bool(await redis.hset(f"{VK_PREFIX}{key_id}", "is_blocked", "1"))


async def unblock_key(key_id: str) -> bool:
    redis = await get_redis()
    return bool(await redis.hset(f"{VK_PREFIX}{key_id}", "is_blocked", "0"))


async def delete_key(key_id: str) -> bool:
    """Delete a key permanently. Spend logs are retained."""
    redis = await get_redis()
    data = await redis.hgetall(f"{VK_PREFIX}{key_id}")
    if not data:
        return False

    key_hash = data.get("key_hash", "")
    pipe = redis.pipeline()
    pipe.delete(f"{VK_PREFIX}{key_id}")
    if key_hash:
        pipe.delete(f"{VK_HASH_PREFIX}{key_hash}")
    await pipe.execute()
    return True


async def get_key_info(key_id: str) -> dict[str, Any] | None:
    redis = await get_redis()
    data = await redis.hgetall(f"{VK_PREFIX}{key_id}")
    if not data:
        return None

    spend = float(await redis.get(f"gateway:spend:key:{key_id}") or "0")
    max_budget = float(data["max_budget"]) if data.get("max_budget") else None

    return {
        **data,
        "current_spend": spend,
        "budget_remaining": (max_budget - spend) if max_budget is not None else None,
        "model_allowlist": json.loads(data["model_allowlist"]) if data.get("model_allowlist") else None,
        "metadata": json.loads(data.get("metadata", "{}")),
    }


async def list_keys(
    user_id: str | None = None,
    team_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List keys, optionally filtered by user_id or team_id."""
    redis = await get_redis()
    keys = []
    cursor = 0
    while True:
        cursor, found = await redis.scan(cursor, match=f"{VK_PREFIX}*", count=200)
        for rkey in found:
            if rkey.startswith(VK_HASH_PREFIX):
                continue
            data = await redis.hgetall(rkey)
            if not data:
                continue
            if user_id and data.get("user_id") != user_id:
                continue
            if team_id and data.get("team_id") != team_id:
                continue
            keys.append({
                "key_id": data.get("key_id"),
                "user_id": data.get("user_id"),
                "team_id": data.get("team_id") or None,
                "key_alias": data.get("key_alias") or None,
                "model_allowlist": json.loads(data["model_allowlist"]) if data.get("model_allowlist") else None,
                "max_budget": float(data["max_budget"]) if data.get("max_budget") else None,
                "is_blocked": data.get("is_blocked") == "1",
                "created_at": float(data.get("created_at", 0)),
            })
            if len(keys) >= limit:
                break
        if cursor == 0 or len(keys) >= limit:
            break

    keys.sort(key=lambda k: k["created_at"], reverse=True)
    return keys[:limit]


async def rotate_key(key_id: str, grace_period_seconds: int = 300) -> tuple[str, str] | None:
    """Generate a new key for the same key_id. Old key remains valid during grace period.

    Returns (new_raw_key, key_id) or None if key not found.
    """
    redis = await get_redis()
    data = await redis.hgetall(f"{VK_PREFIX}{key_id}")
    if not data:
        return None

    old_hash = data.get("key_hash", "")
    new_raw_key = _generate_raw_key()
    new_hash = _hash_key(new_raw_key)

    pipe = redis.pipeline()
    # Update the key record with new hash
    pipe.hset(f"{VK_PREFIX}{key_id}", "key_hash", new_hash)
    # Point new hash to key_id
    pipe.set(f"{VK_HASH_PREFIX}{new_hash}", key_id)
    # Keep old hash valid during grace period then expire
    if old_hash:
        pipe.expire(f"{VK_HASH_PREFIX}{old_hash}", grace_period_seconds)
    await pipe.execute()

    return new_raw_key, key_id


async def update_key(key_id: str, updates: dict[str, Any]) -> bool:
    """Update key constraints. Accepts: max_budget, rpm_limit, tpm_limit, model_allowlist, metadata."""
    redis = await get_redis()
    if not await redis.exists(f"{VK_PREFIX}{key_id}"):
        return False

    mapping = {}
    if "max_budget" in updates:
        mapping["max_budget"] = str(updates["max_budget"]) if updates["max_budget"] is not None else ""
    if "budget_duration" in updates:
        mapping["budget_duration"] = updates["budget_duration"] or ""
    if "rpm_limit" in updates:
        mapping["rpm_limit"] = str(updates["rpm_limit"]) if updates["rpm_limit"] is not None else ""
    if "tpm_limit" in updates:
        mapping["tpm_limit"] = str(updates["tpm_limit"]) if updates["tpm_limit"] is not None else ""
    if "max_parallel_requests" in updates:
        mapping["max_parallel_requests"] = (
            str(updates["max_parallel_requests"]) if updates["max_parallel_requests"] is not None else ""
        )
    if "model_allowlist" in updates:
        mapping["model_allowlist"] = json.dumps(updates["model_allowlist"]) if updates["model_allowlist"] else ""
    if "metadata" in updates:
        mapping["metadata"] = json.dumps(updates["metadata"] or {})

    if mapping:
        await redis.hset(f"{VK_PREFIX}{key_id}", mapping=mapping)
    return True
