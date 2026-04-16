"""Langfuse tracing, Slack alerting, callback monitoring, guardrail dispatch."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings
from schemas.common import GatewayRequestContext, IdentityContext, SpendLog

logger = logging.getLogger(__name__)

_langfuse = None


def _get_langfuse():
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    if not settings.langfuse_public_key:
        return None
    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        return _langfuse
    except Exception:
        logger.exception("Failed to initialize Langfuse")
        _record_callback_error("langfuse_init")
        return None


def on_request_start(ctx: GatewayRequestContext) -> str | None:
    lf = _get_langfuse()
    if not lf:
        return None
    try:
        trace = lf.trace(
            name="gateway-request",
            id=ctx.request_id,
            metadata={
                "model": ctx.model,
                "key_id": ctx.identity.key_id if ctx.identity else "",
                "team_id": ctx.identity.team_id if ctx.identity else "",
                "org_id": ctx.identity.org_id if ctx.identity else "",
            },
            user_id=ctx.identity.user_id if ctx.identity else None,
        )
        return trace.id
    except Exception:
        logger.exception("Langfuse trace creation failed")
        _record_callback_error("langfuse_trace")
        return None


def on_request_success(ctx: GatewayRequestContext, spend_log: SpendLog) -> None:
    lf = _get_langfuse()
    if not lf:
        return
    try:
        lf.generation(
            trace_id=ctx.request_id,
            name="llm-call",
            model=ctx.model,
            metadata={
                "provider": ctx.provider,
                "deployment_id": ctx.deployment_id,
                "cache_hit": ctx.cache_hit,
                "retries_used": ctx.retries_used,
                "fallbacks_used": ctx.fallbacks_used,
            },
            usage={
                "input": spend_log.input_tokens,
                "output": spend_log.output_tokens,
                "total": spend_log.input_tokens + spend_log.output_tokens,
            },
            model_parameters={"cost_usd": spend_log.cost_usd},
        )
    except Exception:
        logger.exception("Langfuse generation logging failed")
        _record_callback_error("langfuse_generation")


def on_request_failure(ctx: GatewayRequestContext, error: Exception) -> None:
    lf = _get_langfuse()
    if not lf:
        return
    try:
        lf.generation(
            trace_id=ctx.request_id,
            name="llm-call",
            model=ctx.model,
            metadata={
                "provider": ctx.provider,
                "error": str(error),
                "retries_used": ctx.retries_used,
            },
            level="ERROR",
            status_message=str(error),
        )
    except Exception:
        logger.exception("Langfuse error logging failed")
        _record_callback_error("langfuse_error")


def log_guardrail_result(
    ctx: GatewayRequestContext,
    guardrail_name: str,
    stage: str,
    passed: bool,
    details: dict | None = None,
) -> None:
    """Log guardrail execution to Langfuse for compliance audit (v1.1)."""
    try:
        from observability.metrics import record_guardrail_result
        record_guardrail_result(guardrail_name, stage, passed)
    except ImportError:
        pass

    lf = _get_langfuse()
    if not lf:
        return
    try:
        lf.event(
            trace_id=ctx.request_id,
            name=f"guardrail-{stage}",
            metadata={
                "guardrail": guardrail_name,
                "passed": passed,
                "details": details or {},
            },
            level="DEFAULT" if passed else "WARNING",
        )
    except Exception:
        _record_callback_error("langfuse_guardrail")


def log_raw_request(
    ctx: GatewayRequestContext,
    url: str,
    headers: dict,
    body: dict,
    response_status: int | None = None,
) -> None:
    """Log raw provider request for debugging (v2)."""
    lf = _get_langfuse()
    if not lf:
        return
    try:
        # Redact auth headers
        safe_headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")}
        lf.event(
            trace_id=ctx.request_id,
            name="raw-provider-request",
            metadata={
                "url": url,
                "headers": safe_headers,
                "body_keys": list(body.keys()),
                "response_status": response_status,
            },
        )
    except Exception:
        _record_callback_error("langfuse_raw_request")


async def send_slack_alert(message: str, level: str = "warning") -> None:
    if not settings.slack_webhook_url:
        return
    emoji = {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}.get(level, ":bell:")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                settings.slack_webhook_url,
                json={"text": f"{emoji} *commandclaw-gateway* | {message}"},
            )
    except Exception:
        logger.exception("Slack alert failed")
        _record_callback_error("slack")


async def send_spend_report_alert(report: dict) -> None:
    """Send periodic spend report to Slack (v1.2)."""
    lines = [f"*Spend Report* ({report.get('period', 'daily')})"]
    for item in report.get("items", [])[:10]:
        lines.append(f"  • {item['label']}: ${item['spend']:.2f}")
    lines.append(f"*Total*: ${report.get('total', 0):.2f}")
    await send_slack_alert("\n".join(lines), "info")


# ── Guardrail dispatch (delegates to middleware/guardrails.py) ──


async def run_pre_call_guardrails(
    request_data: dict[str, Any],
    identity: IdentityContext,
    ctx: GatewayRequestContext | None = None,
) -> tuple[bool, str]:
    """Run pre-call guardrails. Logs results to Langfuse."""
    from middleware.guardrails import run_pre_call_guardrails as _run
    allowed, reason, results = await _run(request_data, identity)

    if ctx and results:
        for r in results:
            log_guardrail_result(ctx, r["guardrail"], "pre", r["passed"], r.get("details"))

    return allowed, reason


async def run_post_call_guardrails(
    response_data: dict[str, Any],
    identity: IdentityContext,
    ctx: GatewayRequestContext | None = None,
) -> tuple[bool, str]:
    """Run post-call guardrails. Logs results to Langfuse."""
    from middleware.guardrails import run_post_call_guardrails as _run
    allowed, reason, results = await _run(response_data, identity)

    if ctx and results:
        for r in results:
            log_guardrail_result(ctx, r["guardrail"], "post", r["passed"], r.get("details"))

    return allowed, reason


def _record_callback_error(callback_type: str) -> None:
    """Track callback delivery failures (v2)."""
    try:
        from observability.metrics import CALLBACK_ERRORS
        CALLBACK_ERRORS.labels(callback_type=callback_type).inc()
    except ImportError:
        pass
