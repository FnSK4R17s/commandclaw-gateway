"""Organization management endpoints — v1.2."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.audit import log_audit_event
from auth.teams import create_org, delete_org, get_org, list_orgs, update_org

router = APIRouter(prefix="/org", tags=["orgs"])


class CreateOrgRequest(BaseModel):
    org_id: str
    name: str
    max_budget: float | None = None
    budget_duration: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    metadata: dict[str, Any] | None = None


class UpdateOrgRequest(BaseModel):
    name: str | None = None
    max_budget: float | None = None
    budget_duration: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    metadata: dict[str, Any] | None = None


@router.post("/new")
async def create(req: CreateOrgRequest, request: Request):
    await create_org(**req.model_dump())
    await log_audit_event("org_created", "admin", "org", req.org_id, {"name": req.name})
    return JSONResponse(content={"org_id": req.org_id, "created": True})


@router.get("/info")
async def info(org_id: str):
    data = await get_org(org_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Organization not found"})
    return JSONResponse(content=data)


@router.get("/list")
async def list_all(limit: int = 100):
    orgs = await list_orgs(limit=limit)
    return JSONResponse(content={"orgs": orgs})


@router.patch("/{org_id}")
async def update(org_id: str, req: UpdateOrgRequest):
    updates = req.model_dump(exclude_none=True)
    ok = await update_org(org_id, updates)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Organization not found"})
    await log_audit_event("org_updated", "admin", "org", org_id, updates)
    return JSONResponse(content={"updated": True})


@router.delete("/{org_id}")
async def delete(org_id: str):
    ok = await delete_org(org_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Organization not found"})
    await log_audit_event("org_deleted", "admin", "org", org_id)
    return JSONResponse(content={"deleted": True})
