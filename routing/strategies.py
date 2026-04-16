"""Advanced routing strategies — v1.1 custom plugin, v2 latency/cost-based.

Also: region filtering, canary splits, context window pre-check, traffic mirroring.
"""

from __future__ import annotations

import importlib
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import redis.asyncio as aioredis

from schemas.common import Deployment, IdentityContext

logger = logging.getLogger(__name__)


# ── v1.1: Custom Routing Plugin ABC ──

class RoutingStrategy(ABC):
    """Base class for custom routing plugins.

    Enterprise implements this, registers via config:
        routing_strategy: custom
        custom_routing_plugin: "mycompany.routing.TierBasedRouter"
    """

    @abstractmethod
    async def select_deployment(
        self,
        deployments: list[Deployment],
        context: RoutingContext,
    ) -> Deployment | None:
        pass


class RoutingContext:
    """Context passed to custom routing plugins."""
    def __init__(self, model: str, identity: IdentityContext, metadata: dict[str, Any] | None = None):
        self.model = model
        self.identity = identity
        self.metadata = metadata or {}


def load_custom_plugin(import_path: str) -> RoutingStrategy:
    """Load a custom routing plugin from a Python import path."""
    module_path, class_name = import_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not issubclass(cls, RoutingStrategy):
        raise TypeError(f"{import_path} must be a subclass of RoutingStrategy")
    return cls()


# ── v1.1: Region Constraint Filter ──

def filter_by_region(
    deployments: list[Deployment],
    allowed_regions: list[str] | None,
    identity_region: str | None,
) -> list[Deployment]:
    """Filter deployments by region constraint. Region constraints are inviolable."""
    constraint = None
    if identity_region:
        constraint = [identity_region]
    elif allowed_regions:
        constraint = allowed_regions

    if not constraint:
        return deployments

    return [d for d in deployments if d.region in constraint]


# ── v1.1: Canary Traffic Splitting ──

def apply_canary_split(
    deployments: list[Deployment],
    canary_config: dict[str, Any] | None,
) -> list[Deployment]:
    """Apply canary traffic splits. Config: {model: {canary_weight: 0.1, canary_deployment_id: "..."}}."""
    if not canary_config:
        return deployments

    for d in deployments:
        override = canary_config.get(d.deployment_id)
        if override:
            d = d.model_copy(update={"weight": override.get("weight", d.weight)})

    return deployments


# ── v1.1: Pre-call Context Window Validation ──

def check_context_window(
    deployment: Deployment,
    estimated_tokens: int,
) -> bool:
    """Reject requests that will exceed model's context limit before sending."""
    if deployment.context_window and estimated_tokens > deployment.context_window:
        return False
    return True


# ── v2: Latency-Based Routing ──

LATENCY_KEY = "gateway:latency:{}"
LATENCY_WINDOW = 300  # 5 minute rolling window


async def record_latency(deployment_id: str, latency_ms: float, redis: aioredis.Redis) -> None:
    """Record a latency sample for a deployment."""
    key = LATENCY_KEY.format(deployment_id)
    now = time.time()
    pipe = redis.pipeline()
    pipe.zadd(key, {f"{now}:{latency_ms}": now})
    pipe.zremrangebyscore(key, 0, now - LATENCY_WINDOW)
    pipe.expire(key, LATENCY_WINDOW + 60)
    await pipe.execute()


async def get_p50_latency(deployment_id: str, redis: aioredis.Redis) -> float | None:
    """Get the p50 latency for a deployment over the rolling window."""
    key = LATENCY_KEY.format(deployment_id)
    now = time.time()
    members = await redis.zrangebyscore(key, now - LATENCY_WINDOW, "+inf")
    if not members:
        return None

    latencies = []
    for m in members:
        parts = str(m).split(":")
        if len(parts) >= 2:
            try:
                latencies.append(float(parts[1]))
            except ValueError:
                pass

    if not latencies:
        return None

    latencies.sort()
    idx = len(latencies) // 2
    return latencies[idx]


async def latency_based_select(
    deployments: list[Deployment],
    redis: aioredis.Redis,
) -> Deployment:
    """Select deployment with lowest p50 latency."""
    best = deployments[0]
    best_latency = float("inf")

    for d in deployments:
        p50 = await get_p50_latency(d.deployment_id, redis)
        if p50 is not None and p50 < best_latency:
            best_latency = p50
            best = d

    return best


# ── v2: Cost-Based Routing ──

def cost_based_select(
    deployments: list[Deployment],
    pricing_table: dict[str, dict[str, float]],
) -> Deployment:
    """Select the cheapest deployment that can serve the request."""
    cheapest = deployments[0]
    cheapest_cost = float("inf")

    for d in deployments:
        pricing = pricing_table.get(d.provider_model) or pricing_table.get(d.model_name)
        if pricing:
            cost = pricing.get("input", 0) + pricing.get("output", 0)
            if cost < cheapest_cost:
                cheapest_cost = cost
                cheapest = d

    return cheapest


# ── v2: Traffic Mirroring (Shadow Requests) ──

async def send_mirror_request(
    deployment: Deployment,
    body: dict[str, Any],
    provider_module,
) -> None:
    """Send a silent background request to a mirror deployment for A/B evaluation."""
    try:
        provider = provider_module.get_provider(deployment.provider)
        url = provider.get_complete_url(deployment)
        headers = provider.get_headers(deployment)
        transformed = provider.transform_request(body, deployment)
        transformed.pop("stream", None)
        await provider.send_request(url, headers, transformed, timeout=60.0)
    except Exception:
        logger.debug("Mirror request failed for %s (expected for shadow traffic)", deployment.deployment_id)


# ── v2: Token Bucket Rate Limiter ──

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1]) or capacity
local last_refill = tonumber(data[2]) or now

-- Refill tokens
local elapsed = now - last_refill
local new_tokens = elapsed * refill_rate
tokens = math.min(capacity, tokens + new_tokens)

-- Try to consume
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


async def token_bucket_check(
    key: str,
    capacity: int,
    refill_rate: float,
    requested: int,
    redis: aioredis.Redis,
) -> tuple[bool, float]:
    """Token bucket rate limit check. Returns (allowed, remaining_tokens)."""
    now = time.time()
    result = await redis.eval(
        _TOKEN_BUCKET_LUA, 1, key,
        str(capacity), str(refill_rate), str(now), str(requested),
    )
    return bool(int(result[0])), float(result[1])


# ── v2: Priority Tiers + Fair Queuing ──

PRIORITY_LEVELS = {"critical": 4, "high": 3, "normal": 2, "low": 1}


def get_priority(identity: IdentityContext) -> int:
    """Get the priority level for a request based on identity metadata."""
    priority_name = identity.metadata.get("priority", "normal")
    return PRIORITY_LEVELS.get(priority_name, 2)
