"""POST /v1/embeddings — embedding proxy with full pipeline enforcement."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.budgets import check_budget
from config import get_config
from infra.redis_client import get_redis
from infra.token_counter import count_tokens_text
from middleware.cost_tracker import extract_usage_from_response, record_spend
from middleware.rate_limiter import check_and_increment_rate_limit
from observability.callbacks import on_request_start
from observability.metrics import record_error, record_request_metrics
from providers import get_provider
from routing.router import deployment_busy_counter, select_deployment
from schemas.common import ErrorDetail, ErrorResponse, GatewayRequestContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["embeddings"])


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    identity = request.state.identity
    request_id = request.state.request_id
    redis = await get_redis()
    config = get_config()

    model_name = body.get("model", "")

    # Estimate input tokens
    input_data = body.get("input", "")
    if isinstance(input_data, str):
        estimated_tokens = count_tokens_text(input_data, model_name)
    elif isinstance(input_data, list):
        estimated_tokens = sum(count_tokens_text(s, model_name) for s in input_data if isinstance(s, str))
    else:
        estimated_tokens = 0

    ctx = GatewayRequestContext(
        request_id=request_id,
        model=model_name,
        identity=identity,
    )

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
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error=ErrorDetail(code="budget_exhausted", message=budget_reason, type="budget_error")
            ).model_dump(),
        )

    # 3. Route
    deployment = await select_deployment(model_name, identity, redis, config)
    if deployment is None:
        record_error(ctx, "no_deployment")
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error=ErrorDetail(code="no_deployment", message=f"No deployment for '{model_name}'", type="server_error")
            ).model_dump(),
        )

    ctx.provider = deployment.provider
    ctx.deployment_id = deployment.deployment_id

    provider = get_provider(deployment.provider)
    url = provider.get_complete_url(deployment, endpoint="embeddings")
    headers = provider.get_headers(deployment)
    body["model"] = deployment.provider_model

    try:
        provider_start = time.monotonic()
        async with deployment_busy_counter(deployment.deployment_id, redis):
            response = await provider.send_request(url, headers, body, timeout=config.router_settings.timeout)
        provider_latency = time.monotonic() - provider_start

        if response.status_code != 200:
            record_error(ctx, f"provider_{response.status_code}")
            return JSONResponse(status_code=response.status_code, content=response.json())

        response_data = response.json()
        input_tokens, output_tokens = extract_usage_from_response(response_data, deployment.provider)
        spend_log = await record_spend(ctx, input_tokens, output_tokens)

        latency = time.monotonic() - ctx.start_time
        record_request_metrics(ctx, "success", latency, provider_latency)

        return JSONResponse(
            content=response_data,
            headers={
                "x-gateway-request-id": request_id,
                "x-litellm-response-cost": str(spend_log.cost_usd),
                **rl_headers,
            },
        )
    except Exception as e:
        record_error(ctx, "internal_error")
        logger.exception("Embedding request failed")
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(
                error=ErrorDetail(code="provider_error", message=str(e), type="server_error")
            ).model_dump(),
        )
