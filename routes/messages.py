"""POST /v1/messages — Anthropic Messages API format endpoint."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from auth.budgets import check_budget
from config import get_config
from infra.redis_client import get_redis
from infra.token_counter import count_tokens_for_request
from middleware.cache import (
    build_cache_key,
    get_cached_response,
    parse_cache_control,
    rechunk_for_streaming,
    set_cached_response,
)
from middleware.cost_tracker import extract_usage_from_response, record_spend
from middleware.rate_limiter import check_and_increment_rate_limit
from observability.callbacks import on_request_start, run_pre_call_guardrails
from observability.metrics import record_cache_hit, record_cache_miss, record_error
from routes.chat import _route_and_call
from schemas.common import GatewayRequestContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["messages"])


def _anthropic_to_openai_request(body: dict[str, Any]) -> dict[str, Any]:
    """Convert Anthropic request format to OpenAI format for internal processing."""
    messages = []

    # System as first message
    if body.get("system"):
        messages.append({"role": "system", "content": body["system"]})

    # Convert messages
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Anthropic content blocks -> OpenAI format
            text_parts = []
            tool_calls = []
            tool_results = []

            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })
                    elif btype == "tool_result":
                        tool_results.append(block)

            if tool_results:
                for tr in tool_results:
                    tc_content = tr.get("content", "")
                    if isinstance(tc_content, list):
                        tc_content = " ".join(b.get("text", "") for b in tc_content if isinstance(b, dict))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": tc_content,
                    })
            elif tool_calls:
                messages.append({
                    "role": role,
                    "content": "\n".join(text_parts) if text_parts else None,
                    "tool_calls": tool_calls,
                })
            else:
                messages.append({"role": role, "content": "\n".join(text_parts)})

    # Build OpenAI request
    openai_req: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": messages,
    }

    if body.get("max_tokens"):
        openai_req["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        openai_req["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        openai_req["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        openai_req["stop"] = body["stop_sequences"]
    if body.get("stream"):
        openai_req["stream"] = body["stream"]
    if body.get("metadata"):
        openai_req["metadata"] = body["metadata"]

    # Convert tools
    if body.get("tools"):
        openai_req["tools"] = []
        for tool in body["tools"]:
            openai_req["tools"].append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })

    return openai_req


def _openai_to_anthropic_response(openai_resp: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert OpenAI response to Anthropic Messages format."""
    choices = openai_resp.get("choices", [])
    content_blocks = []

    if choices:
        message = choices[0].get("message", {})
        text = message.get("content")
        if text:
            content_blocks.append({"type": "text", "text": text})

        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "input": args,
                })

    # Map finish_reason to stop_reason
    finish_reason = choices[0].get("finish_reason", "stop") if choices else "stop"
    stop_reason_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    usage = openai_resp.get("usage", {})

    return {
        "id": openai_resp.get("id", "msg_gateway"),
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        },
    }


@router.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    identity = request.state.identity
    request_id = request.state.request_id
    redis = await get_redis()
    config = get_config()

    model_name = body.get("model", "")
    is_stream = body.get("stream", False)

    # Convert to OpenAI format for internal processing
    openai_body = _anthropic_to_openai_request(body)
    estimated_tokens = count_tokens_for_request(openai_body, model_name)

    ctx = GatewayRequestContext(
        request_id=request_id,
        model=model_name,
        identity=identity,
    )

    on_request_start(ctx)

    # Rate limit
    allowed, rl_headers = await check_and_increment_rate_limit(
        identity, model_name, estimated_tokens, request_id, redis,
    )
    if not allowed:
        record_error(ctx, "rate_limit")
        return JSONResponse(status_code=429, content={
            "type": "error", "error": {"type": "rate_limit_error", "message": "Rate limit exceeded"},
        })

    # Budget
    budget_ok, budget_reason = await check_budget(identity)
    if not budget_ok:
        record_error(ctx, "budget_exhausted")
        return JSONResponse(status_code=429, content={
            "type": "error", "error": {"type": "invalid_request_error", "message": budget_reason},
        })

    # Cache
    cache_control = parse_cache_control(openai_body)
    cache_key = None
    if not cache_control.get("no_cache"):
        cache_key = build_cache_key(
            openai_body,
            namespace=cache_control.get("namespace"),
            cache_scope=config.cache_settings.cache_scope,
            team_id=identity.team_id,
            key_id=identity.key_id,
        )
        cached = await get_cached_response(cache_key, redis, s_maxage=cache_control.get("s_maxage"))
        if cached:
            ctx.cache_hit = True
            record_cache_hit(model_name, identity.team_id or "")
            cached.pop("_cached_at", None)

            if is_stream:
                return StreamingResponse(
                    rechunk_for_streaming(cached, "anthropic"),
                    media_type="text/event-stream",
                )
            else:
                return JSONResponse(content=_openai_to_anthropic_response(cached, model_name))
        else:
            record_cache_miss(model_name, identity.team_id or "")

    # Guardrail
    guard_ok, guard_reason = await run_pre_call_guardrails(openai_body, identity, ctx)
    if not guard_ok:
        return JSONResponse(status_code=400, content={
            "type": "error", "error": {"type": "invalid_request_error", "message": guard_reason},
        })

    tags = body.get("metadata", {}).get("tags", []) if body.get("metadata") else []

    # Non-streaming path
    if not is_stream:
        result = await _route_and_call(openai_body, ctx, redis, config)
        if result is None:
            return JSONResponse(status_code=503, content={
                "type": "error", "error": {"type": "api_error", "message": f"No deployment for '{model_name}'"},
            })

        response_data, _ = result

        # Post-call guardrail
        from observability.callbacks import run_post_call_guardrails
        guard_ok, guard_reason = await run_post_call_guardrails(response_data, identity, ctx)
        if not guard_ok:
            return JSONResponse(status_code=400, content={
                "type": "error", "error": {"type": "invalid_request_error", "message": guard_reason},
            })

        input_tokens, output_tokens = extract_usage_from_response(response_data, ctx.provider or "openai")
        await record_spend(ctx, input_tokens, output_tokens, tags=tags)

        if cache_key and not cache_control.get("no_store"):
            await set_cached_response(cache_key, response_data, model_name, redis)

        return JSONResponse(content=_openai_to_anthropic_response(response_data, model_name))

    # Streaming path — re-emit as Anthropic typed events
    # For streaming, delegate to the chat streaming and convert output
    async def anthropic_stream():
        from routes.chat import _route_and_call
        result = await _route_and_call(openai_body, ctx, redis, config)
        if result is None:
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': 'No deployment available'}})}\n\n"
            return

        response_data, _ = result
        # Convert to Anthropic response and stream as events
        anthropic_resp = _openai_to_anthropic_response(response_data, model_name)
        usage = anthropic_resp.get("usage", {})

        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {**anthropic_resp, 'content': [], 'stop_reason': None, 'usage': {'input_tokens': usage.get('input_tokens', 0), 'output_tokens': 0}}})}\n\n"

        for i, block in enumerate(anthropic_resp.get("content", [])):
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': i, 'content_block': {'type': block['type'], **({'text': ''} if block['type'] == 'text' else {'id': block.get('id', ''), 'name': block.get('name', ''), 'input': {}})}})}\n\n"

            if block["type"] == "text":
                text = block.get("text", "")
                chunk_size = 20
                for j in range(0, max(1, len(text)), chunk_size):
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'text_delta', 'text': text[j:j+chunk_size]}})}\n\n"

            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"

        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': anthropic_resp.get('stop_reason', 'end_turn')}, 'usage': {'output_tokens': usage.get('output_tokens', 0)}})}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

        # Cost tracking
        input_tokens, output_tokens = extract_usage_from_response(response_data, ctx.provider or "openai")
        await record_spend(ctx, input_tokens, output_tokens, tags=tags)

    return StreamingResponse(anthropic_stream(), media_type="text/event-stream")


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    body = await request.json()
    openai_body = _anthropic_to_openai_request(body)
    model = body.get("model", "")
    token_count = count_tokens_for_request(openai_body, model)
    return JSONResponse(content={"input_tokens": token_count})
