"""Background health checks and deployment monitoring — v2.

Proactively checks deployment health rather than waiting for failures.
Detects provider outages, sends alerts, updates deployment state metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from config import get_config
from infra.redis_client import get_redis
from observability.callbacks import send_slack_alert
from observability.metrics import DEPLOYMENT_STATE
from providers import get_provider
from routing.cooldowns import is_cooled_down

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 60  # seconds
_health_task: asyncio.Task | None = None


async def _check_deployment_health(deployment) -> tuple[bool, float]:
    """Check if a deployment is responsive."""
    try:
        provider = get_provider(deployment.provider)
        url = provider.get_complete_url(deployment)
        headers = provider.get_headers(deployment)

        # Lightweight request — models list or minimal completion
        if deployment.provider in ("openai", "groq", "deepseek"):
            check_url = provider.get_complete_url(deployment, endpoint="models")
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(check_url, headers=headers)
            latency = time.monotonic() - start
            return resp.status_code == 200, latency
        else:
            # For non-OpenAI, just check the base URL is reachable
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url.rsplit("/", 1)[0], headers=headers)
            latency = time.monotonic() - start
            return resp.status_code < 500, latency
    except Exception:
        return False, 0.0


async def _health_check_loop() -> None:
    """Background loop that probes all deployments periodically."""
    while True:
        try:
            config = get_config()
            redis = await get_redis()

            for deployment in config.deployments:
                is_cd = await is_cooled_down(deployment.deployment_id, redis)
                healthy, latency = await _check_deployment_health(deployment)

                state = 0 if (is_cd or not healthy) else 1
                DEPLOYMENT_STATE.labels(
                    deployment_id=deployment.deployment_id,
                    model=deployment.model_name,
                    provider=deployment.provider,
                ).set(state)

                if not healthy and not is_cd:
                    logger.warning("Deployment %s health check failed", deployment.deployment_id)
                    await send_slack_alert(
                        f"Deployment `{deployment.deployment_id}` ({deployment.provider}/{deployment.provider_model}) "
                        f"failed health check",
                        "critical",
                    )

                # Record latency for latency-based routing (v2)
                if healthy and latency > 0:
                    from routing.strategies import record_latency
                    await record_latency(deployment.deployment_id, latency * 1000, redis)

        except Exception:
            logger.exception("Health check loop error")

        await asyncio.sleep(HEALTH_CHECK_INTERVAL)


def start_health_checks() -> asyncio.Task:
    global _health_task
    _health_task = asyncio.create_task(_health_check_loop())
    return _health_task


async def get_deployment_health() -> list[dict]:
    """Get current health status of all deployments."""
    config = get_config()
    redis = await get_redis()
    results = []

    for d in config.deployments:
        is_cd = await is_cooled_down(d.deployment_id, redis)
        busy = int(await redis.get(f"gateway:busy:{d.deployment_id}") or "0")
        results.append({
            "deployment_id": d.deployment_id,
            "model": d.model_name,
            "provider": d.provider,
            "region": d.region,
            "cooled_down": is_cd,
            "active_requests": busy,
            "healthy": not is_cd,
        })

    return results
