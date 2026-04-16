"""Budget enforcement and spend tracking — full hierarchical (org > team > user > key)."""

from __future__ import annotations

import asyncio
import logging
import time

from infra.redis_client import get_redis
from schemas.common import IdentityContext

logger = logging.getLogger(__name__)

# Lua script for atomic multi-level spend increment
_INCREMENT_SPEND_LUA = """
local cost = tonumber(ARGV[1])
local results = {}
for i = 1, #KEYS do
    local val = redis.call('INCRBYFLOAT', KEYS[i], cost)
    table.insert(results, tostring(val))
end
return results
"""


async def get_spend(key_id: str) -> float:
    redis = await get_redis()
    val = await redis.get(f"gateway:spend:key:{key_id}")
    return float(val) if val else 0.0


async def get_user_spend(user_id: str) -> float:
    redis = await get_redis()
    val = await redis.get(f"gateway:spend:user:{user_id}")
    return float(val) if val else 0.0


async def get_team_spend(team_id: str) -> float:
    redis = await get_redis()
    val = await redis.get(f"gateway:spend:team:{team_id}")
    return float(val) if val else 0.0


async def get_org_spend(org_id: str) -> float:
    redis = await get_redis()
    val = await redis.get(f"gateway:spend:org:{org_id}")
    return float(val) if val else 0.0


async def increment_spend(
    key_id: str,
    user_id: str,
    cost: float,
    team_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, float]:
    """Atomically increment spend at all applicable levels."""
    redis = await get_redis()
    keys = [f"gateway:spend:key:{key_id}", f"gateway:spend:user:{user_id}"]
    if team_id:
        keys.append(f"gateway:spend:team:{team_id}")
    if org_id:
        keys.append(f"gateway:spend:org:{org_id}")

    result = await redis.eval(_INCREMENT_SPEND_LUA, len(keys), *keys, str(cost))
    output = {"key_spend": float(result[0]), "user_spend": float(result[1])}
    if team_id and len(result) > 2:
        output["team_spend"] = float(result[2])
    if org_id and len(result) > 3:
        output["org_spend"] = float(result[3])
    return output


async def check_budget(identity: IdentityContext, estimated_cost: float = 0.0) -> tuple[bool, str]:
    """Check budget at all hierarchy levels. Blocked if ANY level exhausted."""
    # Key-level
    if identity.max_budget is not None:
        key_spend = await get_spend(identity.key_id)
        if (key_spend + estimated_cost) > identity.max_budget:
            return False, f"Key budget exhausted: ${key_spend:.4f} spent of ${identity.max_budget:.4f} limit"

    # User-level
    if identity.user_id:
        redis = await get_redis()
        user_data = await redis.hgetall(f"gateway:user:{identity.user_id}")
        user_max = user_data.get("max_budget", "") if user_data else ""
        if user_max:
            user_spend = await get_user_spend(identity.user_id)
            if (user_spend + estimated_cost) > float(user_max):
                return False, f"User budget exhausted: ${user_spend:.4f} of ${float(user_max):.4f}"

    # Team-level (v1.1)
    if identity.team_id:
        redis = await get_redis()
        team_data = await redis.hgetall(f"gateway:team:{identity.team_id}")
        team_max = team_data.get("max_budget", "") if team_data else ""
        if team_max:
            team_spend = await get_team_spend(identity.team_id)
            if (team_spend + estimated_cost) > float(team_max):
                return False, f"Team budget exhausted: ${team_spend:.4f} of ${float(team_max):.4f}"

    # Org-level (v1.2)
    if identity.org_id:
        redis = await get_redis()
        org_data = await redis.hgetall(f"gateway:org:{identity.org_id}")
        org_max = org_data.get("max_budget", "") if org_data else ""
        if org_max:
            org_spend = await get_org_spend(identity.org_id)
            if (org_spend + estimated_cost) > float(org_max):
                return False, f"Organization budget exhausted: ${org_spend:.4f} of ${float(org_max):.4f}"

    # Model-specific budgets (v1.1)
    model_budget = identity.metadata.get("model_budgets", {})
    if model_budget:
        # Checked by the caller which knows the model name
        pass

    return True, ""


async def check_model_budget(
    identity: IdentityContext,
    model: str,
    estimated_cost: float = 0.0,
) -> tuple[bool, str]:
    """Check per-model budget cap."""
    model_budgets = identity.metadata.get("model_budgets", {})
    if not model_budgets or model not in model_budgets:
        return True, ""

    redis = await get_redis()
    key = f"gateway:spend:key:{identity.key_id}:model:{model}"
    current = float(await redis.get(key) or "0")
    limit = float(model_budgets[model])
    if (current + estimated_cost) > limit:
        return False, f"Model budget exhausted for {model}: ${current:.4f} of ${limit:.4f}"
    return True, ""


async def check_session_budget(
    session_id: str,
    max_iterations: int,
    max_cost: float,
) -> tuple[bool, str]:
    """Check agent iteration caps / session budgets (v1.1)."""
    redis = await get_redis()
    iters = int(await redis.get(f"gateway:session:{session_id}:iterations") or "0")
    cost = float(await redis.get(f"gateway:session:{session_id}:cost") or "0")

    if max_iterations and iters >= max_iterations:
        return False, f"Session iteration limit ({max_iterations}) reached"
    if max_cost and cost >= max_cost:
        return False, f"Session cost limit (${max_cost}) reached"
    return True, ""


async def increment_session(session_id: str, cost: float) -> None:
    """Track session iterations and cost."""
    redis = await get_redis()
    pipe = redis.pipeline()
    pipe.incr(f"gateway:session:{session_id}:iterations")
    pipe.incrbyfloat(f"gateway:session:{session_id}:cost", cost)
    pipe.expire(f"gateway:session:{session_id}:iterations", 86400)
    pipe.expire(f"gateway:session:{session_id}:cost", 86400)
    await pipe.execute()


async def reset_budget(key_id: str) -> bool:
    redis = await get_redis()
    await redis.set(f"gateway:spend:key:{key_id}", "0")
    return True


async def reset_all_spend() -> bool:
    """Reset all spend counters (v2 — testing/fiscal period resets)."""
    redis = await get_redis()
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="gateway:spend:*", count=200)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break
    return True


async def _budget_reset_loop() -> None:
    """Background task that resets budgets when their reset_at timestamp passes."""
    while True:
        try:
            redis = await get_redis()
            now = time.time()
            cursor = 0
            while True:
                cursor, keys = await redis.scan(cursor, match="gateway:vk:*", count=100)
                for rkey in keys:
                    if ":hash:" in rkey:
                        continue
                    data = await redis.hgetall(rkey)
                    reset_at = data.get("budget_reset_at", "")
                    if not reset_at:
                        continue
                    if float(reset_at) <= now:
                        key_id = data.get("key_id", "")
                        duration = data.get("budget_duration", "")
                        if key_id:
                            await reset_budget(key_id)
                            if duration == "daily":
                                next_reset = now + 86400
                            elif duration == "monthly":
                                next_reset = now + 2592000
                            else:
                                continue
                            await redis.hset(rkey, "budget_reset_at", str(next_reset))
                            logger.info("Budget reset for key %s", key_id)
                if cursor == 0:
                    break
        except Exception:
            logger.exception("Budget reset loop error")
        await asyncio.sleep(60)


def start_budget_scheduler() -> asyncio.Task:
    return asyncio.create_task(_budget_reset_loop())
