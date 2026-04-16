"""RBAC enforcement — role-based access control for gateway operations.

Roles:
  - proxy_admin: full access to all endpoints
  - team_admin: manage keys/users within their team
  - internal_user: use LLM endpoints, view own key info
"""

from __future__ import annotations

from schemas.common import IdentityContext

ROLE_HIERARCHY = {
    "proxy_admin": 3,
    "team_admin": 2,
    "internal_user": 1,
}

# Permissions: (method, path_prefix) -> minimum role
# All keys are strictly 2-tuples. For sub-path patterns like /key/{id}/regenerate,
# the prefix "/key/" is sufficient since the method narrows the match.
ENDPOINT_PERMISSIONS: dict[tuple[str, str], str] = {
    # Key operations
    ("POST", "/key/generate"): "internal_user",  # constrained by upperbound
    ("POST", "/key/block"): "team_admin",
    ("POST", "/key/unblock"): "team_admin",
    ("DELETE", "/key/"): "team_admin",
    ("PATCH", "/key/"): "team_admin",
    ("POST", "/key/"): "team_admin",  # covers /key/{id}/regenerate
    # Team management
    ("POST", "/team/new"): "proxy_admin",
    ("DELETE", "/team/"): "proxy_admin",
    ("PATCH", "/team/"): "team_admin",
    # Org management
    ("POST", "/org/new"): "proxy_admin",
    ("DELETE", "/org/"): "proxy_admin",
    ("PATCH", "/org/"): "proxy_admin",
    # Spend management
    ("POST", "/global/spend/reset"): "proxy_admin",
    ("DELETE", "/cache/"): "team_admin",
}


def check_rbac(identity: IdentityContext, method: str, path: str) -> tuple[bool, str]:
    """Check if the identity has permission for the given operation.

    Returns (allowed, reason).
    """
    user_level = ROLE_HIERARCHY.get(identity.user_role, 1)

    # Proxy admin bypasses all checks
    if user_level >= 3:
        return True, ""

    # Check explicit endpoint permissions — find the most specific match
    matched_role: str | None = None
    matched_prefix_len = 0

    for (req_method, path_prefix), min_role in ENDPOINT_PERMISSIONS.items():
        if method == req_method and path.startswith(path_prefix):
            # Prefer the longest (most specific) prefix match
            if len(path_prefix) > matched_prefix_len:
                matched_prefix_len = len(path_prefix)
                matched_role = min_role

    if matched_role:
        required_level = ROLE_HIERARCHY.get(matched_role, 1)
        if user_level < required_level:
            return False, f"Role '{identity.user_role}' cannot perform {method} {path} (requires '{matched_role}')"

    # Team admins can only manage their own team's resources
    if user_level == 2:
        # Enforced at the route handler level (key's team must match)
        pass

    return True, ""


def check_key_generation_bounds(
    identity: IdentityContext,
    requested_budget: float | None,
    requested_rpm: int | None,
    requested_tpm: int | None,
) -> tuple[bool, str]:
    """Enforce upperbound constraints on key generation.

    Non-admin users cannot create keys with higher limits than their own.
    """
    if ROLE_HIERARCHY.get(identity.user_role, 1) >= 3:
        return True, ""  # Admins bypass

    if requested_budget is not None and identity.max_budget is not None:
        if requested_budget > identity.max_budget:
            return False, f"Cannot create key with budget ${requested_budget} (your limit: ${identity.max_budget})"

    if requested_rpm is not None and identity.rpm_limit is not None:
        if requested_rpm > identity.rpm_limit:
            return False, f"Cannot create key with {requested_rpm} RPM (your limit: {identity.rpm_limit})"

    if requested_tpm is not None and identity.tpm_limit is not None:
        if requested_tpm > identity.tpm_limit:
            return False, f"Cannot create key with {requested_tpm} TPM (your limit: {identity.tpm_limit})"

    return True, ""
