"""Spend log queries and cache management endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from infra.redis_client import get_redis, redis_health_check
from middleware.cache import delete_cache
from middleware.cost_tracker import get_spend_logs

router = APIRouter(tags=["spend"])


@router.get("/spend/logs")
async def spend_logs(
    key_id: str | None = None,
    user_id: str | None = None,
    tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    logs = await get_spend_logs(
        key_id=key_id, user_id=user_id, tag=tag,
        limit=limit, offset=offset,
    )
    return JSONResponse(content={"logs": logs, "count": len(logs)})


@router.get("/cache/ping")
async def cache_ping():
    ok = await redis_health_check()
    return JSONResponse(content={"status": "ok" if ok else "error"})


@router.delete("/cache/delete")
async def cache_delete(model: str | None = None, cache_key: str | None = None):
    redis = await get_redis()
    deleted = await delete_cache(redis, model=model, cache_key=cache_key)
    return JSONResponse(content={"deleted": deleted})


@router.delete("/cache/flush")
async def cache_flush():
    redis = await get_redis()
    deleted = await delete_cache(redis)
    return JSONResponse(content={"deleted": deleted})
