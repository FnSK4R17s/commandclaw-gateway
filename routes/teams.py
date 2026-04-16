"""Team management endpoints — v1.1."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.audit import log_audit_event
from auth.teams import (
    add_team_member, create_team, delete_team, get_team,
    list_teams, remove_team_member, update_team,
)

router = APIRouter(prefix="/team", tags=["teams"])


class CreateTeamRequest(BaseModel):
    team_id: str
    name: str
    org_id: str | None = None
    max_budget: float | None = None
    budget_duration: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    model_allowlist: list[str] | None = None
    guardrail_policy: str | None = None
    allowed_regions: list[str] | None = None
    metadata: dict[str, Any] | None = None


class UpdateTeamRequest(BaseModel):
    name: str | None = None
    max_budget: float | None = None
    budget_duration: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    model_allowlist: list[str] | None = None
    guardrail_policy: str | None = None
    allowed_regions: list[str] | None = None
    metadata: dict[str, Any] | None = None


@router.post("/new")
async def create(req: CreateTeamRequest, request: Request):
    await create_team(**req.model_dump())
    await log_audit_event("team_created", "admin", "team", req.team_id, {"name": req.name})
    return JSONResponse(content={"team_id": req.team_id, "created": True})


@router.get("/info")
async def info(team_id: str):
    data = await get_team(team_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Team not found"})
    return JSONResponse(content=data)


@router.get("/list")
async def list_all(org_id: str | None = None, limit: int = 100):
    teams = await list_teams(org_id=org_id, limit=limit)
    return JSONResponse(content={"teams": teams})


@router.patch("/{team_id}")
async def update(team_id: str, req: UpdateTeamRequest):
    updates = req.model_dump(exclude_none=True)
    ok = await update_team(team_id, updates)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Team not found"})
    await log_audit_event("team_updated", "admin", "team", team_id, updates)
    return JSONResponse(content={"updated": True})


@router.delete("/{team_id}")
async def delete(team_id: str):
    ok = await delete_team(team_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Team not found"})
    await log_audit_event("team_deleted", "admin", "team", team_id)
    return JSONResponse(content={"deleted": True})


@router.post("/{team_id}/member")
async def add_member(team_id: str, user_id: str, role: str = "member"):
    ok = await add_team_member(team_id, user_id, role)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Team not found"})
    await log_audit_event("member_added", "admin", "team", team_id, {"user_id": user_id, "role": role})
    return JSONResponse(content={"added": True})


@router.delete("/{team_id}/member/{user_id}")
async def remove_member(team_id: str, user_id: str):
    ok = await remove_team_member(team_id, user_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "Team not found"})
    return JSONResponse(content={"removed": True})
