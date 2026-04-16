"""Global spend reporting and management endpoints — v1.2/v2."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.budgets import reset_all_spend
from middleware.cost_tracker import get_spend_logs

router = APIRouter(prefix="/global", tags=["global"])


@router.get("/spend/report")
async def spend_report(
    group_by: str = "key",  # key, user, team, model, tag
    limit: int = 50,
):
    """Aggregated spend report grouped by dimension (v1.2)."""
    logs = await get_spend_logs(limit=10000)
    aggregated: dict[str, float] = {}

    for log in logs:
        if group_by == "key":
            key = log.get("key_id", "unknown")
        elif group_by == "user":
            key = log.get("user_id", "unknown")
        elif group_by == "team":
            key = log.get("team_id", "unknown") or "no_team"
        elif group_by == "model":
            key = log.get("model", "unknown")
        elif group_by == "tag":
            tags = log.get("tags", [])
            for tag in tags:
                aggregated[tag] = aggregated.get(tag, 0) + log.get("cost_usd", 0)
            continue
        else:
            key = log.get("key_id", "unknown")

        aggregated[key] = aggregated.get(key, 0) + log.get("cost_usd", 0)

    sorted_items = sorted(aggregated.items(), key=lambda x: x[1], reverse=True)[:limit]
    total = sum(v for _, v in sorted_items)

    return JSONResponse(content={
        "group_by": group_by,
        "items": [{"label": k, "spend": round(v, 6)} for k, v in sorted_items],
        "total": round(total, 6),
    })


@router.get("/spend/daily")
async def daily_activity(user_id: str | None = None, days: int = 7):
    """Daily activity breakdown (v2)."""
    import time
    logs = await get_spend_logs(user_id=user_id, limit=50000)

    daily: dict[str, dict[str, Any]] = {}
    for log in logs:
        ts = log.get("created_at", 0)
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if day not in daily:
            daily[day] = {"date": day, "requests": 0, "tokens": 0, "cost": 0.0}
        daily[day]["requests"] += 1
        daily[day]["tokens"] += log.get("input_tokens", 0) + log.get("output_tokens", 0)
        daily[day]["cost"] += log.get("cost_usd", 0)

    sorted_days = sorted(daily.values(), key=lambda x: x["date"], reverse=True)[:days]
    return JSONResponse(content={"activity": sorted_days})


@router.post("/spend/reset")
async def reset_spend(request: Request):
    """Reset all spend counters (v2 — testing/fiscal period resets)."""
    await reset_all_spend()
    return JSONResponse(content={"reset": True})
