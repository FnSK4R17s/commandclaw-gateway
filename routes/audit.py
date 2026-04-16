"""Audit log endpoints — v1.2."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from auth.audit import get_audit_logs

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs")
async def logs(
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    entries = await get_audit_logs(
        resource_type=resource_type, resource_id=resource_id,
        actor_id=actor_id, action=action, limit=limit, offset=offset,
    )
    return JSONResponse(content={"logs": entries, "count": len(entries)})
