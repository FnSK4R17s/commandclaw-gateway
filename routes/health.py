"""Health and metrics endpoints — no auth required."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest

from config import get_config
from infra.redis_client import redis_health_check

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    redis_ok = await redis_health_check()
    config = get_config()
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": "connected" if redis_ok else "disconnected",
        "models_loaded": len(config.get_all_model_names()),
    }


@router.get("/health/readiness")
async def readiness():
    redis_ok = await redis_health_check()
    if not redis_ok:
        return PlainTextResponse("Redis not ready", status_code=503)
    return {"status": "ready"}


@router.get("/health/liveliness")
async def liveliness():
    return {"status": "ok"}


@router.get("/metrics")
async def metrics():
    return PlainTextResponse(
        generate_latest().decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
