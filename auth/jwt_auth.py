"""JWT/OIDC authentication — v1.1 pluggable auth backend."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt as pyjwt

from config import settings
from schemas.common import IdentityContext

logger = logging.getLogger(__name__)

_jwks_cache: dict[str, Any] | None = None
_jwks_fetched_at: float = 0


async def _fetch_jwks(jwks_uri: str) -> dict[str, Any]:
    """Fetch JWKS from the OIDC provider."""
    global _jwks_cache, _jwks_fetched_at
    now = time.time()
    if _jwks_cache and (now - _jwks_fetched_at) < 3600:
        return _jwks_cache

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_fetched_at = now
        return _jwks_cache


async def validate_jwt(token: str) -> IdentityContext | None:
    """Validate a JWT token and return IdentityContext.

    Expects settings to have jwt_* fields configured.
    """
    try:
        jwt_issuer = getattr(settings, "jwt_issuer", "")
        jwt_audience = getattr(settings, "jwt_audience", "")
        jwks_uri = getattr(settings, "jwt_jwks_uri", "")
        jwt_secret = getattr(settings, "jwt_secret", "")

        if jwt_secret:
            # Simple symmetric validation
            payload = pyjwt.decode(
                token, jwt_secret,
                algorithms=["HS256"],
                audience=jwt_audience or None,
                issuer=jwt_issuer or None,
            )
        elif jwks_uri:
            # Asymmetric validation with JWKS
            jwks = await _fetch_jwks(jwks_uri)
            header = pyjwt.get_unverified_header(token)
            kid = header.get("kid")

            key = None
            for k in jwks.get("keys", []):
                if k.get("kid") == kid:
                    key = pyjwt.algorithms.RSAAlgorithm.from_jwk(k)
                    break

            if not key:
                logger.warning("No matching JWK found for kid=%s", kid)
                return None

            payload = pyjwt.decode(
                token, key,
                algorithms=["RS256", "RS384", "RS512"],
                audience=jwt_audience or None,
                issuer=jwt_issuer or None,
            )
        else:
            logger.error("JWT auth enabled but no jwt_secret or jwt_jwks_uri configured")
            return None

        return IdentityContext(
            key_id=f"jwt:{payload.get('sub', 'unknown')}",
            user_id=payload.get("sub", ""),
            team_id=payload.get("team_id") or payload.get("groups", [None])[0] if payload.get("groups") else None,
            org_id=payload.get("org_id"),
            key_alias=payload.get("name") or payload.get("email"),
            user_role=payload.get("role", "internal_user"),
            model_allowlist=payload.get("allowed_models"),
            max_budget=payload.get("max_budget"),
            rpm_limit=payload.get("rpm_limit"),
            tpm_limit=payload.get("tpm_limit"),
            guardrail_policy=payload.get("guardrail_policy"),
            region_constraint=payload.get("region_constraint"),
            metadata={"jwt_claims": payload},
        )
    except pyjwt.ExpiredSignatureError:
        logger.debug("JWT expired")
        return None
    except pyjwt.InvalidTokenError as e:
        logger.debug("Invalid JWT: %s", e)
        return None
    except Exception:
        logger.exception("JWT validation failed")
        return None
