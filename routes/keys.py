"""Virtual key management endpoints — requires master key auth."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.virtual_keys import (
    block_key,
    delete_key,
    generate_key,
    get_key_info,
    list_keys,
    rotate_key,
    unblock_key,
    update_key,
)

router = APIRouter(prefix="/key", tags=["keys"])


class GenerateKeyRequest(BaseModel):
    user_id: str
    models: list[str] | None = None
    max_budget: float | None = None
    budget_duration: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel_requests: int | None = None
    team_id: str | None = None
    org_id: str | None = None
    key_alias: str | None = None
    guardrail_policy: str | None = None
    metadata: dict[str, Any] | None = None
    expires_in: int | None = None


class UpdateKeyRequest(BaseModel):
    max_budget: float | None = None
    budget_duration: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel_requests: int | None = None
    model_allowlist: list[str] | None = None
    metadata: dict[str, Any] | None = None


@router.post("/generate")
async def generate(req: GenerateKeyRequest, request: Request):
    raw_key, info = await generate_key(
        user_id=req.user_id,
        models=req.models,
        max_budget=req.max_budget,
        budget_duration=req.budget_duration,
        rpm_limit=req.rpm_limit,
        tpm_limit=req.tpm_limit,
        max_parallel_requests=req.max_parallel_requests,
        team_id=req.team_id,
        org_id=req.org_id,
        key_alias=req.key_alias,
        guardrail_policy=req.guardrail_policy,
        metadata=req.metadata,
        expires_in=req.expires_in,
    )
    return JSONResponse(content={
        "key": raw_key,
        "key_id": info["key_id"],
        "user_id": info["user_id"],
        "max_budget": req.max_budget,
        "models": req.models,
        "expires_in": req.expires_in,
    })


@router.get("/info")
async def info(key_id: str):
    data = await get_key_info(key_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Key not found"})
    return JSONResponse(content=data)


@router.get("/list")
async def list_all(user_id: str | None = None, team_id: str | None = None, limit: int = 100):
    keys = await list_keys(user_id=user_id, team_id=team_id, limit=limit)
    return JSONResponse(content={"keys": keys})


@router.post("/block")
async def block(key_id: str):
    result = await block_key(key_id)
    return JSONResponse(content={"blocked": result, "key_id": key_id})


@router.post("/unblock")
async def unblock(key_id: str):
    result = await unblock_key(key_id)
    return JSONResponse(content={"unblocked": result, "key_id": key_id})


@router.delete("/{key_id}")
async def delete(key_id: str):
    result = await delete_key(key_id)
    if not result:
        return JSONResponse(status_code=404, content={"error": "Key not found"})
    return JSONResponse(content={"deleted": True, "key_id": key_id})


@router.post("/{key_id}/regenerate")
async def regenerate(key_id: str, grace_period: int = 300):
    result = await rotate_key(key_id, grace_period_seconds=grace_period)
    if not result:
        return JSONResponse(status_code=404, content={"error": "Key not found"})
    new_key, kid = result
    return JSONResponse(content={"key": new_key, "key_id": kid, "grace_period_seconds": grace_period})


@router.patch("/{key_id}")
async def update(key_id: str, req: UpdateKeyRequest):
    updates = req.model_dump(exclude_none=True)
    result = await update_key(key_id, updates)
    if not result:
        return JSONResponse(status_code=404, content={"error": "Key not found"})
    return JSONResponse(content={"updated": True, "key_id": key_id})
