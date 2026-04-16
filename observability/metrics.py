"""Prometheus metrics — all carry team/org/key/model labels from v1.

Includes v1 core, v1.1 cache/budget gauges, v2 remaining gauges.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from schemas.common import GatewayRequestContext

# ── Counters ──

REQUEST_TOTAL = Counter(
    "gateway_requests_total",
    "Total requests processed",
    ["model", "provider", "key_id", "team_id", "org_id", "status"],
)

FAILED_REQUESTS = Counter(
    "gateway_failed_requests_total",
    "Failed requests by error type",
    ["model", "provider", "error_type", "key_id", "team_id", "org_id"],
)

TOKENS_INPUT = Counter(
    "gateway_input_tokens_total",
    "Input tokens processed",
    ["model", "provider", "key_id", "team_id", "org_id"],
)

TOKENS_OUTPUT = Counter(
    "gateway_output_tokens_total",
    "Output tokens generated",
    ["model", "provider", "key_id", "team_id", "org_id"],
)

SPEND = Counter(
    "gateway_spend_usd_total",
    "Total spend in USD",
    ["model", "provider", "key_id", "team_id", "org_id"],
)

CACHE_HITS = Counter(
    "gateway_cache_hits_total",
    "Cache hits",
    ["model", "cache_type", "team_id"],
)

CACHE_MISSES = Counter(
    "gateway_cache_misses_total",
    "Cache misses",
    ["model", "team_id"],
)

CACHE_EVICTIONS = Counter(
    "gateway_cache_evictions_total",
    "Cache evictions",
    [],
)

RATE_LIMIT_HITS = Counter(
    "gateway_rate_limit_hits_total",
    "Rate limit rejections",
    ["key_id", "team_id", "model", "limit_type"],
)

FALLBACK_TOTAL = Counter(
    "gateway_fallbacks_total",
    "Fallback activations",
    ["primary_model", "fallback_model", "fallback_type"],
)

GUARDRAIL_RESULTS = Counter(
    "gateway_guardrail_results_total",
    "Guardrail execution results",
    ["guardrail_name", "stage", "passed"],
)

# ── Histograms ──

REQUEST_LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "End-to-end request latency",
    ["model", "provider", "key_id", "team_id", "org_id"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

PROVIDER_LATENCY = Histogram(
    "gateway_provider_latency_seconds",
    "Provider-only call latency (excludes gateway overhead)",
    ["model", "provider", "key_id", "team_id", "org_id"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

TTFT = Histogram(
    "gateway_time_to_first_token_seconds",
    "Time to first token for streaming requests",
    ["model", "provider"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

CACHE_LATENCY = Histogram(
    "gateway_cache_operation_latency_seconds",
    "Cache get/set latency",
    ["operation"],
)

# ── Gauges ──

DEPLOYMENT_STATE = Gauge(
    "gateway_deployment_state",
    "Deployment health (1=healthy, 0=cooled_down)",
    ["deployment_id", "model", "provider"],
)

CACHE_SIZE_BYTES = Gauge(
    "gateway_cache_size_bytes",
    "Approximate Redis cache memory usage",
    [],
)

BUDGET_REMAINING = Gauge(
    "gateway_budget_remaining_usd",
    "Remaining budget in USD",
    ["key_id", "user_id", "team_id", "org_id"],
)

RATE_LIMIT_REMAINING = Gauge(
    "gateway_rate_limit_remaining",
    "Remaining rate limit capacity",
    ["key_id", "dimension", "limit_type"],
)

ACTIVE_REQUESTS = Gauge(
    "gateway_active_requests",
    "Currently in-flight requests per deployment",
    ["deployment_id", "model", "provider"],
)

CALLBACK_ERRORS = Counter(
    "gateway_callback_errors_total",
    "Callback delivery failures",
    ["callback_type"],
)


def _labels(ctx: GatewayRequestContext) -> dict[str, str]:
    return {
        "model": ctx.model,
        "provider": ctx.provider or "",
        "key_id": ctx.identity.key_id if ctx.identity else "",
        "team_id": ctx.identity.team_id or "" if ctx.identity else "",
        "org_id": ctx.identity.org_id or "" if ctx.identity else "",
    }


def record_request_metrics(
    ctx: GatewayRequestContext,
    status: str,
    latency: float,
    provider_latency: float | None = None,
) -> None:
    labels = _labels(ctx)
    REQUEST_TOTAL.labels(**labels, status=status).inc()
    REQUEST_LATENCY.labels(**labels).observe(latency)

    if provider_latency is not None:
        PROVIDER_LATENCY.labels(**labels).observe(provider_latency)


def record_spend_metrics(
    ctx: GatewayRequestContext,
    input_tokens: int,
    output_tokens: int,
    cost: float,
) -> None:
    labels = _labels(ctx)
    TOKENS_INPUT.labels(**labels).inc(input_tokens)
    TOKENS_OUTPUT.labels(**labels).inc(output_tokens)
    if cost > 0:
        SPEND.labels(**labels).inc(cost)


def record_cache_hit(model: str, team_id: str = "", cache_type: str = "exact") -> None:
    CACHE_HITS.labels(model=model, cache_type=cache_type, team_id=team_id).inc()


def record_cache_miss(model: str, team_id: str = "") -> None:
    CACHE_MISSES.labels(model=model, team_id=team_id).inc()


def record_error(ctx: GatewayRequestContext, error_type: str) -> None:
    labels = _labels(ctx)
    FAILED_REQUESTS.labels(**labels, error_type=error_type).inc()


def record_rate_limit_hit(key_id: str, team_id: str, model: str, limit_type: str) -> None:
    RATE_LIMIT_HITS.labels(key_id=key_id, team_id=team_id, model=model, limit_type=limit_type).inc()


def record_guardrail_result(guardrail_name: str, stage: str, passed: bool) -> None:
    GUARDRAIL_RESULTS.labels(guardrail_name=guardrail_name, stage=stage, passed=str(passed)).inc()


def record_fallback(primary: str, fallback: str, fb_type: str) -> None:
    FALLBACK_TOTAL.labels(primary_model=primary, fallback_model=fallback, fallback_type=fb_type).inc()
