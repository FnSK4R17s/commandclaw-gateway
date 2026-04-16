"""Shared data types used across the gateway."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


class UsageBlock(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Anthropic prompt caching fields — zeroed for non-Anthropic providers
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ErrorDetail(BaseModel):
    code: str
    message: str
    type: str
    param: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class IdentityContext(BaseModel):
    """Loaded from virtual key on every authenticated request.

    Attached to request.state.identity by auth middleware.
    team_id and org_id present from v1 per architectural constraint —
    populated when teams (v1.1) and orgs (v1.2) ship.
    """

    key_id: str
    user_id: str
    team_id: str | None = None
    org_id: str | None = None
    key_alias: str | None = None
    user_role: str = "internal_user"
    model_allowlist: list[str] | None = None
    max_budget: float | None = None
    budget_duration: str | None = None
    budget_remaining: float = float("inf")
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel_requests: int | None = None
    guardrail_policy: str | None = None  # v1.1
    region_constraint: str | None = None  # v1.1
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayRequestContext(BaseModel):
    """Per-request context threaded through the middleware pipeline."""

    request_id: str
    model: str
    identity: IdentityContext
    start_time: float = Field(default_factory=time.monotonic)
    provider: str | None = None
    deployment_id: str | None = None
    cache_hit: bool = False
    retries_used: int = 0
    fallbacks_used: list[str] = Field(default_factory=list)
    trace_id: str | None = None  # Langfuse trace ID


class Deployment(BaseModel):
    """A single model deployment (parsed from config.yaml model_list entry)."""

    deployment_id: str
    model_name: str  # user-facing name (e.g. "gpt-4o")
    provider: str  # openai, anthropic, vertex, bedrock, ollama
    provider_model: str  # provider-specific model ID (e.g. "gpt-4o-2024-08-06")
    api_base: str = ""
    api_key_env: str = ""  # env var name, not the key itself
    rpm: int | None = None
    tpm: int | None = None
    max_parallel_requests: int | None = None
    order: int = 1
    weight: float = 1.0
    region: str | None = None
    context_window: int | None = None
    model_info: dict[str, Any] = Field(default_factory=dict)


class SpendLog(BaseModel):
    """Financial record for a single request."""

    log_id: str
    request_id: str
    key_id: str
    user_id: str
    team_id: str | None = None  # v1: always None. v1.1: populated
    org_id: str | None = None  # v1: always None. v1.2: populated
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cache_hit: bool = False
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
