"""POST /v1/responses — OpenAI Responses API format (v2).

Runs through the full shared pipeline: rate limit, budget, cache, guardrails, routing, spend.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.budgets import check_budget
from config import get_config
from infra.redis_client import get_redis
from infra.token_counter import count_tokens_for_request
from middleware.cache import (
    build_cache_key, get_cached_response, parse_cache_control, set_cached_response,
)
from middleware.cost_tracker import extract_usage_from_response, record_spend
from middleware.rate_limiter import check_and_increment_rate_limit
from observability.callbacks import (
    on_request_failure, on_request_start, on_request_success,
    run_post_call_guardrails, run_pre_call_guardrails,
)
from observability.metrics import record_cache_hit, record_cache_miss, record_error, record_request_metrics
from routes.chat import _route_and_call
from schemas.common import ErrorDetail, ErrorResponse, GatewayRequestContext

router = APIRouter(tags=["responses"])


def _responses_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Convert Responses API format to chat completions format."""
    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    input_data = body.get("input", "")
    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content", "")
                messages.append({"role": role, "content": content})

    chat_req: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": messages,
    }
    if body.get("temperature") is not None:
        chat_req["temperature"] = body["temperature"]
    if body.get("max_output_tokens"):
        chat_req["max_tokens"] = body["max_output_tokens"]
    if body.get("top_p") is not None:
        chat_req["top_p"] = body["top_p"]
    if body.get("tools"):
        chat_req["tools"] = body["tools"]
    if body.get("metadata"):
        chat_req["metadata"] = body["metadata"]

    return chat_req


def _chat_to_responses(chat_resp: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert chat completions response to Responses API format."""
    choices = chat_resp.get("choices", [])
    output = []

    if choices:
        msg = choices[0].get("message", {})
        content = msg.get("content")
        if content:
            output.append({
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:8]}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
                "status": "completed",
            })
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                output.append({
                    "type": "function_call",
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", "{}"),
                    "status": "completed",
                })

    usage = chat_resp.get("usage", {})

    return {
        "id": f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": output,
        "status": "completed",
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


@router.post("/v1/responses")
async def create_response(request: Request):
    body = await request.json()
    identity = request.state.identity
    request_id = request.state.request_id
    redis = await get_redis()
    config = get_config()

    model_name = body.get("model", "")
    chat_body = _responses_to_chat(body)
    estimated_tokens = count_tokens_for_request(chat_body, model_name)

    ctx = GatewayRequestContext(
        request_id=request_id,
        model=model_name,
        identity=identity,
    )

    on_request_start(ctx)
    tags = body.get("metadata", {}).get("tags", []) if body.get("metadata") else []

    # 1. Rate limit
    allowed, rl_headers = await check_and_increment_rate_limit(
        identity, model_name, estimated_tokens, request_id, redis,
    )
    if not allowed:
        record_error(ctx, "rate_limit")
        return JSONResponse(status_code=429, content=ErrorResponse(
            error=ErrorDetail(code="rate_limit_exceeded", message="Rate limit exceeded", type="rate_limit_error")
        ).model_dump(), headers=rl_headers)

    # 2. Budget
    budget_ok, budget_reason = await check_budget(identity)
    if not budget_ok:
        record_error(ctx, "budget_exhausted")
        return JSONResponse(status_code=429, content=ErrorResponse(
            error=ErrorDetail(code="budget_exhausted", message=budget_reason, type="budget_error")
        ).model_dump())

    # 3. Cache
    cache_control = parse_cache_control(chat_body)
    cache_key = None
    if not cache_control.get("no_cache"):
        cache_key = build_cache_key(
            chat_body, namespace=cache_control.get("namespace"),
            cache_scope=config.cache_settings.cache_scope,
            team_id=identity.team_id, key_id=identity.key_id,
        )
        cached = await get_cached_response(cache_key, redis, s_maxage=cache_control.get("s_maxage"))
        if cached:
            ctx.cache_hit = True
            record_cache_hit(model_name, identity.team_id or "")
            cached.pop("_cached_at", None)
            return JSONResponse(content=_chat_to_responses(cached, model_name), headers={
                "x-litellm-response-cost": "0.0", "x-gateway-request-id": request_id, **rl_headers,
            })
        else:
            record_cache_miss(model_name, identity.team_id or "")

    # 4. Pre-call guardrail
    guard_ok, guard_reason = await run_pre_call_guardrails(chat_body, identity, ctx)
    if not guard_ok:
        record_error(ctx, "guardrail_blocked")
        return JSONResponse(status_code=400, content=ErrorResponse(
            error=ErrorDetail(code="guardrail_violation", message=guard_reason, type="guardrail_error")
        ).model_dump())

    # 5. Route + call
    result = await _route_and_call(chat_body, ctx, redis, config)
    if result is None:
        record_error(ctx, "no_deployment")
        on_request_failure(ctx, RuntimeError("No deployment available"))
        return JSONResponse(status_code=503, content=ErrorResponse(
            error=ErrorDetail(code="no_deployment", message=f"No deployment for '{model_name}'", type="server_error")
        ).model_dump())

    response_data, provider_latency = result

    # 6. Post-call guardrail
    guard_ok, guard_reason = await run_post_call_guardrails(response_data, identity, ctx)
    if not guard_ok:
        record_error(ctx, "guardrail_blocked_post")
        return JSONResponse(status_code=400, content=ErrorResponse(
            error=ErrorDetail(code="guardrail_violation", message=guard_reason, type="guardrail_error")
        ).model_dump())

    # 7. Cost tracking
    inp, out = extract_usage_from_response(response_data, ctx.provider or "openai")
    spend_log = await record_spend(ctx, inp, out, tags=tags)

    # 8. Cache write
    if cache_key and not cache_control.get("no_store"):
        await set_cached_response(cache_key, response_data, model_name, redis)

    # 9. Metrics
    latency = time.monotonic() - ctx.start_time
    record_request_metrics(ctx, "success", latency, provider_latency)
    on_request_success(ctx, spend_log)

    return JSONResponse(content=_chat_to_responses(response_data, model_name), headers={
        "x-litellm-response-cost": str(spend_log.cost_usd),
        "x-gateway-request-id": request_id,
        **rl_headers,
    })
