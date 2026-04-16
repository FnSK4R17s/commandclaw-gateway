"""Deployment router — full filter pipeline + all strategies (v1 through v2)."""

from __future__ import annotations

import random
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from config import GatewayConfig, get_pricing_table
from routing.cooldowns import is_cooled_down
from routing.strategies import (
    RoutingContext,
    check_context_window,
    cost_based_select,
    filter_by_region,
    latency_based_select,
    load_custom_plugin,
)
from schemas.common import Deployment, IdentityContext

BUSY_KEY = "gateway:busy:{}"

_custom_plugin = None


async def select_deployment(
    model_name: str,
    identity: IdentityContext,
    redis: aioredis.Redis,
    config: GatewayConfig,
    estimated_tokens: int = 0,
    disable_fallbacks: bool = False,
) -> Deployment | None:
    """Select a deployment using the filter pipeline + strategy.

    Pipeline:
      1. Get all deployments for the model
      2. Filter: model allowlist
      3. Filter: remove cooled-down deployments
      4. Filter: region constraint (v1.1)
      5. Filter: upstream provider rate limit awareness (v1.1)
      6. Filter: context window pre-check (v1.1)
      7. Apply routing strategy
    """
    deployments = config.get_deployments_for_model(model_name)
    if not deployments:
        return None

    # Model allowlist
    if identity.model_allowlist and model_name not in identity.model_allowlist:
        return None

    # Filter cooled-down
    healthy = []
    for d in deployments:
        if not await is_cooled_down(d.deployment_id, redis):
            healthy.append(d)
    if not healthy:
        return None

    # Region constraint filter (v1.1)
    allowed_regions = None
    if identity.team_id:
        team_data = await redis.hgetall(f"gateway:team:{identity.team_id}")
        if team_data and team_data.get("allowed_regions"):
            import json
            allowed_regions = json.loads(team_data["allowed_regions"])

    healthy = filter_by_region(healthy, allowed_regions, identity.region_constraint)
    if not healthy:
        return None

    # Upstream provider rate limit awareness (v1.1)
    from middleware.rate_limiter import get_upstream_remaining
    non_depleted = []
    for d in healthy:
        remaining = await get_upstream_remaining(d.deployment_id, redis)
        if remaining is None or remaining > 0:
            non_depleted.append(d)
    if non_depleted:
        healthy = non_depleted
    # If all depleted, try them anyway (best effort)

    # Context window pre-check (v1.1)
    if estimated_tokens > 0:
        fits = [d for d in healthy if check_context_window(d, estimated_tokens)]
        if fits:
            healthy = fits

    # Max parallel requests check
    if identity.max_parallel_requests:
        busy_count = 0
        for d in healthy:
            c = int(await redis.get(BUSY_KEY.format(d.deployment_id)) or "0")
            busy_count += c
        if busy_count >= identity.max_parallel_requests:
            return None

    # Apply routing strategy
    strategy = config.router_settings.routing_strategy

    if strategy == "least-busy":
        return await _least_busy(healthy, redis)
    elif strategy == "latency-based":
        return await latency_based_select(healthy, redis)
    elif strategy == "cost-based":
        return cost_based_select(healthy, get_pricing_table())
    elif strategy == "custom":
        return await _custom_strategy(healthy, model_name, identity, config)
    else:
        return _simple_shuffle(healthy)


def _simple_shuffle(deployments: list[Deployment]) -> Deployment:
    weights = [d.weight for d in deployments]
    return random.choices(deployments, weights=weights, k=1)[0]


async def _least_busy(deployments: list[Deployment], redis: aioredis.Redis) -> Deployment:
    min_busy = float("inf")
    selected = deployments[0]
    for d in deployments:
        count = int(await redis.get(BUSY_KEY.format(d.deployment_id)) or "0")
        if count < min_busy:
            min_busy = count
            selected = d
    return selected


async def _custom_strategy(
    deployments: list[Deployment],
    model_name: str,
    identity: IdentityContext,
    config: GatewayConfig,
) -> Deployment | None:
    global _custom_plugin
    plugin_path = getattr(config.router_settings, "custom_routing_plugin", None)
    if not plugin_path:
        return _simple_shuffle(deployments)

    if _custom_plugin is None:
        _custom_plugin = load_custom_plugin(plugin_path)

    ctx = RoutingContext(model=model_name, identity=identity)
    return await _custom_plugin.select_deployment(deployments, ctx)


@asynccontextmanager
async def deployment_busy_counter(deployment_id: str, redis: aioredis.Redis):
    key = BUSY_KEY.format(deployment_id)
    await redis.incr(key)
    await redis.expire(key, 300)
    try:
        yield
    finally:
        await redis.decr(key)
