"""Auth middleware — authenticate then authorize. API key + JWT/OIDC."""

from __future__ import annotations

import logging

import ulid
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from auth.virtual_keys import validate_key
from config import get_config, settings
from schemas.common import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({
    "/health", "/health/readiness", "/health/liveliness",
    "/metrics", "/", "/docs", "/openapi.json",
})


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    x_api_key = request.headers.get("x-api-key", "")
    if x_api_key:
        return x_api_key.strip()
    return None


def _is_admin_path(path: str) -> bool:
    return (path.startswith("/key/") or path.startswith("/user/")
            or path.startswith("/spend/") or path.startswith("/cache/")
            or path.startswith("/team/") or path.startswith("/org/")
            or path.startswith("/audit/") or path.startswith("/global/"))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path.rstrip("/") or "/"

        if path in PUBLIC_PATHS:
            return await call_next(request)

        raw_key = _extract_key(request)
        if not raw_key:
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error=ErrorDetail(code="unauthorized", message="Missing API key", type="auth_error")
                ).model_dump(),
            )

        # Step 1: Authenticate — resolve identity from key or JWT
        # Master key gets a synthetic admin identity
        identity = None
        if raw_key == settings.gateway_master_key:
            from schemas.common import IdentityContext
            identity = IdentityContext(
                key_id="master",
                user_id="admin",
                user_role="proxy_admin",
            )
        else:
            # Try JWT if configured
            config = get_config()
            if config.auth_strategy == "jwt":
                from auth.jwt_auth import validate_jwt
                identity = await validate_jwt(raw_key)
            # Fall back to virtual key
            if identity is None:
                identity = await validate_key(raw_key)

        if not identity:
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error=ErrorDetail(code="invalid_key", message="Invalid or expired API key", type="auth_error")
                ).model_dump(),
            )

        # Step 2: Authorize — RBAC check for admin write operations
        if _is_admin_path(path) and request.method not in ("GET", "HEAD", "OPTIONS"):
            from auth.rbac import check_rbac
            rbac_ok, rbac_reason = check_rbac(identity, request.method, path)
            if not rbac_ok:
                return JSONResponse(
                    status_code=403,
                    content=ErrorResponse(
                        error=ErrorDetail(code="forbidden", message=rbac_reason, type="auth_error")
                    ).model_dump(),
                )

        request.state.identity = identity
        request.state.request_id = str(ulid.ULID())

        return await call_next(request)
