"""Audit trail — immutable log of key/team/org operations. v1.2."""

from __future__ import annotations

import json
import time
from typing import Any

from infra.redis_client import get_redis

AUDIT_LOG_KEY = "gateway:audit_log"
AUDIT_LOG_CAP = 50_000


async def log_audit_event(
    action: str,
    actor_id: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Append an audit event to the immutable log."""
    redis = await get_redis()
    event = {
        "action": action,
        "actor_id": actor_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": details or {},
        "timestamp": time.time(),
    }
    pipe = redis.pipeline()
    pipe.lpush(AUDIT_LOG_KEY, json.dumps(event))
    pipe.ltrim(AUDIT_LOG_KEY, 0, AUDIT_LOG_CAP - 1)
    await pipe.execute()


async def get_audit_logs(
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    redis = await get_redis()
    # Fetch a larger window for filtering
    raw = await redis.lrange(AUDIT_LOG_KEY, 0, offset + limit * 5)
    results = []
    skipped = 0
    for entry in raw:
        try:
            event = json.loads(entry)
        except (json.JSONDecodeError, TypeError):
            continue
        if resource_type and event.get("resource_type") != resource_type:
            continue
        if resource_id and event.get("resource_id") != resource_id:
            continue
        if actor_id and event.get("actor_id") != actor_id:
            continue
        if action and event.get("action") != action:
            continue
        if skipped < offset:
            skipped += 1
            continue
        results.append(event)
        if len(results) >= limit:
            break
    return results
