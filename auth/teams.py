"""Team and organization hierarchy management — v1.1/v1.2."""

from __future__ import annotations

import json
import time
from typing import Any

from infra.redis_client import get_redis

TEAM_PREFIX = "gateway:team:"
ORG_PREFIX = "gateway:org:"


# ── Teams (v1.1) ──


async def create_team(
    team_id: str,
    name: str,
    org_id: str | None = None,
    max_budget: float | None = None,
    budget_duration: str | None = None,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    model_allowlist: list[str] | None = None,
    guardrail_policy: str | None = None,
    allowed_regions: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    redis = await get_redis()
    data = {
        "team_id": team_id,
        "name": name,
        "org_id": org_id or "",
        "max_budget": str(max_budget) if max_budget is not None else "",
        "budget_duration": budget_duration or "",
        "rpm_limit": str(rpm_limit) if rpm_limit is not None else "",
        "tpm_limit": str(tpm_limit) if tpm_limit is not None else "",
        "model_allowlist": json.dumps(model_allowlist) if model_allowlist else "",
        "guardrail_policy": guardrail_policy or "",
        "allowed_regions": json.dumps(allowed_regions) if allowed_regions else "",
        "members": json.dumps([]),
        "metadata": json.dumps(metadata or {}),
        "created_at": str(time.time()),
    }
    await redis.hset(f"{TEAM_PREFIX}{team_id}", mapping=data)
    return data


async def get_team(team_id: str) -> dict[str, Any] | None:
    redis = await get_redis()
    data = await redis.hgetall(f"{TEAM_PREFIX}{team_id}")
    if not data:
        return None
    spend = float(await redis.get(f"gateway:spend:team:{team_id}") or "0")
    return {
        **data,
        "current_spend": spend,
        "model_allowlist": json.loads(data["model_allowlist"]) if data.get("model_allowlist") else None,
        "allowed_regions": json.loads(data["allowed_regions"]) if data.get("allowed_regions") else None,
        "members": json.loads(data.get("members", "[]")),
        "metadata": json.loads(data.get("metadata", "{}")),
    }


async def update_team(team_id: str, updates: dict[str, Any]) -> bool:
    redis = await get_redis()
    if not await redis.exists(f"{TEAM_PREFIX}{team_id}"):
        return False
    mapping = {}
    for field in ["name", "max_budget", "budget_duration", "rpm_limit", "tpm_limit",
                  "guardrail_policy"]:
        if field in updates:
            val = updates[field]
            mapping[field] = str(val) if val is not None else ""
    if "model_allowlist" in updates:
        mapping["model_allowlist"] = json.dumps(updates["model_allowlist"]) if updates["model_allowlist"] else ""
    if "allowed_regions" in updates:
        mapping["allowed_regions"] = json.dumps(updates["allowed_regions"]) if updates["allowed_regions"] else ""
    if "metadata" in updates:
        mapping["metadata"] = json.dumps(updates["metadata"] or {})
    if mapping:
        await redis.hset(f"{TEAM_PREFIX}{team_id}", mapping=mapping)
    return True


async def delete_team(team_id: str) -> bool:
    redis = await get_redis()
    return bool(await redis.delete(f"{TEAM_PREFIX}{team_id}"))


async def list_teams(org_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    redis = await get_redis()
    teams = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"{TEAM_PREFIX}*", count=200)
        for key in keys:
            data = await redis.hgetall(key)
            if not data:
                continue
            if org_id and data.get("org_id") != org_id:
                continue
            teams.append({
                "team_id": data.get("team_id"),
                "name": data.get("name"),
                "org_id": data.get("org_id") or None,
                "max_budget": float(data["max_budget"]) if data.get("max_budget") else None,
            })
            if len(teams) >= limit:
                break
        if cursor == 0 or len(teams) >= limit:
            break
    return teams


async def add_team_member(team_id: str, user_id: str, role: str = "member") -> bool:
    redis = await get_redis()
    data = await redis.hgetall(f"{TEAM_PREFIX}{team_id}")
    if not data:
        return False
    members = json.loads(data.get("members", "[]"))
    for m in members:
        if m.get("user_id") == user_id:
            m["role"] = role
            await redis.hset(f"{TEAM_PREFIX}{team_id}", "members", json.dumps(members))
            return True
    members.append({"user_id": user_id, "role": role, "added_at": time.time()})
    await redis.hset(f"{TEAM_PREFIX}{team_id}", "members", json.dumps(members))
    return True


async def remove_team_member(team_id: str, user_id: str) -> bool:
    redis = await get_redis()
    data = await redis.hgetall(f"{TEAM_PREFIX}{team_id}")
    if not data:
        return False
    members = json.loads(data.get("members", "[]"))
    members = [m for m in members if m.get("user_id") != user_id]
    await redis.hset(f"{TEAM_PREFIX}{team_id}", "members", json.dumps(members))
    return True


# ── Organizations (v1.2) ──


async def create_org(
    org_id: str,
    name: str,
    max_budget: float | None = None,
    budget_duration: str | None = None,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    redis = await get_redis()
    data = {
        "org_id": org_id,
        "name": name,
        "max_budget": str(max_budget) if max_budget is not None else "",
        "budget_duration": budget_duration or "",
        "rpm_limit": str(rpm_limit) if rpm_limit is not None else "",
        "tpm_limit": str(tpm_limit) if tpm_limit is not None else "",
        "metadata": json.dumps(metadata or {}),
        "created_at": str(time.time()),
    }
    await redis.hset(f"{ORG_PREFIX}{org_id}", mapping=data)
    return data


async def get_org(org_id: str) -> dict[str, Any] | None:
    redis = await get_redis()
    data = await redis.hgetall(f"{ORG_PREFIX}{org_id}")
    if not data:
        return None
    spend = float(await redis.get(f"gateway:spend:org:{org_id}") or "0")
    return {**data, "current_spend": spend, "metadata": json.loads(data.get("metadata", "{}"))}


async def update_org(org_id: str, updates: dict[str, Any]) -> bool:
    redis = await get_redis()
    if not await redis.exists(f"{ORG_PREFIX}{org_id}"):
        return False
    mapping = {}
    for field in ["name", "max_budget", "budget_duration", "rpm_limit", "tpm_limit"]:
        if field in updates:
            val = updates[field]
            mapping[field] = str(val) if val is not None else ""
    if "metadata" in updates:
        mapping["metadata"] = json.dumps(updates["metadata"] or {})
    if mapping:
        await redis.hset(f"{ORG_PREFIX}{org_id}", mapping=mapping)
    return True


async def delete_org(org_id: str) -> bool:
    redis = await get_redis()
    return bool(await redis.delete(f"{ORG_PREFIX}{org_id}"))


async def list_orgs(limit: int = 100) -> list[dict[str, Any]]:
    redis = await get_redis()
    orgs = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=f"{ORG_PREFIX}*", count=200)
        for key in keys:
            data = await redis.hgetall(key)
            if data:
                orgs.append({
                    "org_id": data.get("org_id"),
                    "name": data.get("name"),
                    "max_budget": float(data["max_budget"]) if data.get("max_budget") else None,
                })
            if len(orgs) >= limit:
                break
        if cursor == 0 or len(orgs) >= limit:
            break
    return orgs
