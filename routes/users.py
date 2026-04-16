"""User management endpoints."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.budgets import get_user_spend
from infra.redis_client import get_redis

router = APIRouter(prefix="/user", tags=["users"])


class CreateUserRequest(BaseModel):
    user_id: str
    max_budget: float | None = None
    metadata: dict[str, Any] | None = None


@router.post("/new")
async def create_user(req: CreateUserRequest, request: Request):
    redis = await get_redis()
    user_data = {
        "user_id": req.user_id,
        "max_budget": str(req.max_budget) if req.max_budget is not None else "",
        "metadata": json.dumps(req.metadata or {}),
        "created_at": str(time.time()),
    }
    await redis.hset(f"gateway:user:{req.user_id}", mapping=user_data)
    return JSONResponse(content={"user_id": req.user_id, "created": True})


@router.get("/info")
async def user_info(user_id: str):
    redis = await get_redis()
    data = await redis.hgetall(f"gateway:user:{user_id}")
    if not data:
        return JSONResponse(status_code=404, content={"error": "User not found"})

    spend = await get_user_spend(user_id)
    max_budget = float(data["max_budget"]) if data.get("max_budget") else None

    return JSONResponse(content={
        "user_id": data.get("user_id"),
        "max_budget": max_budget,
        "current_spend": spend,
        "budget_remaining": (max_budget - spend) if max_budget is not None else None,
        "metadata": json.loads(data.get("metadata", "{}")),
        "created_at": float(data.get("created_at", 0)),
    })
