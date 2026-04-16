"""POST /v1/chat/completions — OpenAI format, streaming + non-streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from auth.budgets import check_budget
from config import get_config
from infra.redis_client import get_redis
from infra.token_counter import count_tokens_for_request
from middleware.cache import (
    StreamingCacheAssembler,
    build_cache_key,
    get_cached_response,
    parse_cache_control,
    rechunk_for_streaming,
    set_cached_response,
)
from middleware.cost_tracker import extract_usage_from_response, record_spend
from middleware.rate_limiter import check_and_increment_rate_limit
from observability.callbacks import (
    on_request_failure,
    on_request_start,
    on_request_success,
    run_post_call_guardrails,
    run_pre_call_guardrails,
    send_slack_alert,
)
from observability.metrics import record_cache_hit, record_cache_miss, record_error, record_request_metrics
from providers import get_provider
from providers.base import ProviderError
from routing.cooldowns import increment_failure, maybe_cooldown, reset_cooldown
from routing.fallbacks import FallbackChain
from routing.retries import compute_backoff, is_context_window_error, parse_retry_after, should_retry
from routing.router import deployment_busy_counter, select_deployment
from schemas.common import ErrorDetail, ErrorResponse, GatewayRequestContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    identity = request.state.identity
    request_id = request.state.request_id
    redis = await get_redis()
    config = get_config()

    model_name = body.get("model", "")
    is_stream = body.get("stream", False)
    estimated_tokens = count_tokens_for_request(body, model_name)

    ctx = GatewayRequestContext(
        request_id=request_id,
        model=model_name,
        identity=identity,
    )

    # Langfuse trace
    on_request_start(ctx)

    # 1. Rate limit
    allowed, rl_headers = await check_and_increment_rate_limit(
        identity, model_name, estimated_tokens, request_id, redis,
    )
    if not allowed:
        record_error(ctx, "rate_limit")
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error=ErrorDetail(code="rate_limit_exceeded", message="Rate limit exceeded", type="rate_limit_error")
            ).model_dump(),
            headers=rl_headers,
        )

    # 2. Budget check
    budget_ok, budget_reason = await check_budget(identity)
    if not budget_ok:
        record_error(ctx, "budget_exhausted")
        await send_slack_alert(f"Budget exhausted for key {identity.key_id}: {budget_reason}", "warning")
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error=ErrorDetail(code="budget_exhausted", message=budget_reason, type="budget_error")
            ).model_dump(),
        )

    # 3. Cache check
    cache_control = parse_cache_control(body)
    cache_key = None
    if not cache_control.get("no_cache"):
        cache_key = build_cache_key(
            body,
            namespace=cache_control.get("namespace"),
            cache_scope=config.cache_settings.cache_scope,
            team_id=identity.team_id,
            key_id=identity.key_id,
        )
        cached = await get_cached_response(cache_key, redis, s_maxage=cache_control.get("s_maxage"))
        if cached:
            ctx.cache_hit = True
            record_cache_hit(model_name, identity.team_id or "")

            if is_stream:
                return StreamingResponse(
                    rechunk_for_streaming(cached, "openai"),
                    media_type="text/event-stream",
                    headers={
                        "x-gateway-cache-key": cache_key,
                        "x-gateway-request-id": request_id,
                        "x-litellm-response-cost": "0.0",
                        **rl_headers,
                    },
                )
            else:
                cached.pop("_cached_at", None)
                return JSONResponse(
                    content=cached,
                    headers={
                        "x-gateway-cache-key": cache_key,
                        "x-gateway-request-id": request_id,
                        "x-litellm-response-cost": "0.0",
                        **rl_headers,
                    },
                )
        else:
            record_cache_miss(model_name, identity.team_id or "")

    # 4. Pre-call guardrail
    guard_ok, guard_reason = await run_pre_call_guardrails(body, identity, ctx)
    if not guard_ok:
        record_error(ctx, "guardrail_blocked")
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error=ErrorDetail(code="guardrail_violation", message=guard_reason, type="guardrail_error")
            ).model_dump(),
        )

    # 5. Route + call with retry/fallback loop
    tags = body.get("metadata", {}).get("tags", []) if body.get("metadata") else []

    if is_stream:
        return await _handle_streaming(body, ctx, redis, config, cache_key, cache_control, rl_headers, tags)
    else:
        return await _handle_non_streaming(body, ctx, redis, config, cache_key, cache_control, rl_headers, tags)


async def _handle_non_streaming(
    body: dict[str, Any],
    ctx: GatewayRequestContext,
    redis,
    config,
    cache_key: str | None,
    cache_control: dict[str, Any],
    rl_headers: dict[str, str],
    tags: list[str],
) -> JSONResponse:
    """Non-streaming request with retry/fallback."""
    result = await _route_and_call(body, ctx, redis, config)

    if result is None:
        record_error(ctx, "no_deployment")
        on_request_failure(ctx, RuntimeError("No deployment available"))
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error=ErrorDetail(code="no_deployment", message=f"No healthy deployment for model '{ctx.model}'", type="server_error")
            ).model_dump(),
        )

    response_data, provider_latency = result

    # Post-call guardrail — block return if policy rejects the response
    guard_ok, guard_reason = await run_post_call_guardrails(response_data, ctx.identity, ctx)
    if not guard_ok:
        record_error(ctx, "guardrail_blocked_post")
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error=ErrorDetail(code="guardrail_violation", message=guard_reason, type="guardrail_error")
            ).model_dump(),
        )

    # Cost tracking
    input_tokens, output_tokens = extract_usage_from_response(response_data, ctx.provider or "openai")
    spend_log = await record_spend(ctx, input_tokens, output_tokens, tags=tags)

    # Cache write (only after guardrails pass)
    if cache_key and not cache_control.get("no_store"):
        await set_cached_response(cache_key, response_data, ctx.model, redis, ttl_override=cache_control.get("ttl"))

    # Metrics
    latency = time.monotonic() - ctx.start_time
    record_request_metrics(ctx, "success", latency, provider_latency)
    on_request_success(ctx, spend_log)

    return JSONResponse(
        content=response_data,
        headers={
            "x-litellm-response-cost": str(spend_log.cost_usd),
            "x-gateway-request-id": ctx.request_id,
            "x-gateway-model": ctx.deployment_id or "",
            **({"x-gateway-cache-key": cache_key} if cache_key else {}),
            **rl_headers,
        },
    )


async def _handle_streaming(
    body: dict[str, Any],
    ctx: GatewayRequestContext,
    redis,
    config,
    cache_key: str | None,
    cache_control: dict[str, Any],
    rl_headers: dict[str, str],
    tags: list[str],
):
    """Streaming request — returns StreamingResponse."""

    async def stream_generator():
        deployment = await select_deployment(ctx.model, ctx.identity, redis, config)
        if deployment is None:
            error = {"error": {"message": f"No healthy deployment for model '{ctx.model}'", "type": "server_error"}}
            yield f"data: {json.dumps(error)}\n\n"
            yield "data: [DONE]\n\n"
            return

        ctx.provider = deployment.provider
        ctx.deployment_id = deployment.deployment_id

        provider = get_provider(deployment.provider)
        url = provider.get_complete_url(deployment)
        headers = provider.get_headers(deployment)
        transformed_body = provider.transform_request(body, deployment)
        transformed_body["stream"] = True

        assembler = StreamingCacheAssembler(
            cache_key or "", ctx.model, redis,
            no_store=cache_control.get("no_store", False) or not cache_key,
        )

        first_chunk = True
        start = time.monotonic()

        try:
            async with deployment_busy_counter(deployment.deployment_id, redis):
                async for line in provider.send_streaming_request(url, headers, transformed_body, config.router_settings.timeout):
                    chunk = provider.transform_stream_chunk(line, deployment)
                    if chunk is None:
                        continue
                    if chunk.get("_done"):
                        break

                    if first_chunk:
                        from observability.metrics import TTFT
                        TTFT.labels(model=ctx.model, provider=ctx.provider or "").observe(time.monotonic() - start)
                        first_chunk = False

                    assembler.add_chunk(chunk)
                    yield f"data: {json.dumps(chunk)}\n\n"

            yield "data: [DONE]\n\n"

            # Finalize cache
            await assembler.finalize()
            await reset_cooldown(deployment.deployment_id, redis)

            # Cost tracking
            usage = assembler.usage or {}
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            spend_log = await record_spend(ctx, input_tokens, output_tokens, tags=tags)

            latency = time.monotonic() - ctx.start_time
            record_request_metrics(ctx, "success", latency)
            on_request_success(ctx, spend_log)

        except ProviderError as e:
            record_error(ctx, f"provider_{e.status_code}")
            on_request_failure(ctx, e)
            error_chunk = {"error": {"message": str(e), "type": "provider_error"}}
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            record_error(ctx, "internal_error")
            on_request_failure(ctx, e)
            logger.exception("Streaming error")
            error_chunk = {"error": {"message": "Internal gateway error", "type": "server_error"}}
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "x-gateway-request-id": ctx.request_id,
            **({"x-gateway-cache-key": cache_key} if cache_key else {}),
            **rl_headers,
        },
    )


async def _route_and_call(
    body: dict[str, Any],
    ctx: GatewayRequestContext,
    redis,
    config,
) -> tuple[dict[str, Any], float] | None:
    """Route to a deployment, call with retries, fallback on failure.

    Returns (response_data, provider_latency) or None.
    """
    models_to_try = [ctx.model]

    # Append fallback models
    fallback_chain = FallbackChain(ctx.model, config)
    # We'll add fallbacks as needed during the loop

    for model_name in models_to_try:
        deployment = await select_deployment(model_name, ctx.identity, redis, config)
        if deployment is None:
            # Try next model in fallback chain
            if model_name == ctx.model and fallback_chain.has_fallbacks():
                models_to_try.extend(fallback_chain.get_fallback_models("standard"))
            continue

        ctx.provider = deployment.provider
        ctx.deployment_id = deployment.deployment_id

        provider = get_provider(deployment.provider)
        url = provider.get_complete_url(deployment)
        headers = provider.get_headers(deployment)
        transformed_body = provider.transform_request(body, deployment)
        # Remove stream for non-streaming
        transformed_body.pop("stream", None)

        # Retry loop
        max_retries = config.router_settings.num_retries
        for attempt in range(max_retries + 1):
            try:
                provider_start = time.monotonic()
                async with deployment_busy_counter(deployment.deployment_id, redis):
                    response = await provider.send_request(
                        url, headers, transformed_body,
                        timeout=config.router_settings.timeout,
                    )
                provider_latency = time.monotonic() - provider_start

                if response.status_code == 200:
                    response_data = response.json()
                    result = provider.transform_response(response_data, deployment)
                    await reset_cooldown(deployment.deployment_id, redis)
                    return result, provider_latency

                # Error handling
                try:
                    error_body = response.json()
                except Exception:
                    error_body = {"error": {"message": response.text}}

                # Context window error -> try context window fallback
                if is_context_window_error(response.status_code, error_body):
                    cw_fallbacks = fallback_chain.get_fallback_models("context_window")
                    for fb in cw_fallbacks:
                        if fb not in models_to_try:
                            models_to_try.append(fb)
                    ctx.fallbacks_used.append(f"context_window:{model_name}")
                    break  # Exit retry loop, try next model

                # Should we retry?
                if should_retry(response.status_code, attempt, max_retries):
                    ctx.retries_used += 1
                    retry_after = parse_retry_after(dict(response.headers))
                    wait = retry_after or compute_backoff(attempt)
                    await asyncio.sleep(wait)
                    continue

                # Non-retryable error
                fail_count = await increment_failure(deployment.deployment_id, redis)
                await maybe_cooldown(
                    deployment.deployment_id, fail_count,
                    config.router_settings.allowed_fails,
                    config.router_settings.cooldown_time, redis,
                )
                break  # Try next model

            except Exception:
                ctx.retries_used += 1
                if attempt < max_retries:
                    await asyncio.sleep(compute_backoff(attempt))
                    continue

                fail_count = await increment_failure(deployment.deployment_id, redis)
                await maybe_cooldown(
                    deployment.deployment_id, fail_count,
                    config.router_settings.allowed_fails,
                    config.router_settings.cooldown_time, redis,
                )
                break

        # If we get here, this model failed — add standard fallbacks
        if model_name == ctx.model and fallback_chain.has_fallbacks():
            fb_models = fallback_chain.get_fallback_models("standard")
            for fb in fb_models:
                if fb not in models_to_try:
                    models_to_try.append(fb)
            ctx.fallbacks_used.append(f"standard:{model_name}")

    return None
