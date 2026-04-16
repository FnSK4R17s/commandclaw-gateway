"""POST /v1/batches — Batch inference endpoint (v2).

Each batch item runs through the full enforcement pipeline:
rate limits, budget, guardrails, routing, cost tracking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.budgets import check_budget
from config import get_config
from infra.redis_client import get_redis
from infra.token_counter import count_tokens_for_request
from middleware.cost_tracker import extract_usage_from_response, record_spend
from middleware.rate_limiter import check_and_increment_rate_limit
from providers import get_provider
from routing.router import deployment_busy_counter, select_deployment
from schemas.common import GatewayRequestContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["batches"])

BATCH_PREFIX = "gateway:batch:"


@router.post("/v1/batches")
async def create_batch(request: Request):
    body = await request.json()
    identity = request.state.identity
    redis = await get_redis()

    requests_list = body.get("requests", [])
    if not requests_list:
        return JSONResponse(status_code=400, content={"error": "No requests provided"})

    # Pre-flight budget check for the whole batch
    budget_ok, budget_reason = await check_budget(identity)
    if not budget_ok:
        return JSONResponse(status_code=429, content={"error": budget_reason})

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    batch_data = {
        "batch_id": batch_id,
        "status": "processing",
        "total": len(requests_list),
        "completed": 0,
        "failed": 0,
        "created_at": time.time(),
    }
    await redis.hset(f"{BATCH_PREFIX}{batch_id}", mapping={k: str(v) for k, v in batch_data.items()})
    await redis.expire(f"{BATCH_PREFIX}{batch_id}", 86400)

    asyncio.create_task(_process_batch(batch_id, requests_list, identity, redis))

    return JSONResponse(content={"batch_id": batch_id, "status": "processing", "total": len(requests_list)})


@router.get("/v1/batches/{batch_id}")
async def get_batch(batch_id: str, request: Request):
    redis = await get_redis()
    data = await redis.hgetall(f"{BATCH_PREFIX}{batch_id}")
    if not data:
        return JSONResponse(status_code=404, content={"error": "Batch not found"})

    results_raw = await redis.lrange(f"{BATCH_PREFIX}{batch_id}:results", 0, -1)
    results = [json.loads(r) for r in results_raw]

    return JSONResponse(content={
        "batch_id": batch_id,
        "status": data.get("status", "unknown"),
        "total": int(data.get("total", 0)),
        "completed": int(data.get("completed", 0)),
        "failed": int(data.get("failed", 0)),
        "results": results,
    })


async def _process_batch(batch_id: str, requests: list[dict], identity, redis):
    config = get_config()
    completed = 0
    failed = 0

    for i, req in enumerate(requests):
        model = req.get("model", "")
        item_id = f"{batch_id}_{i}"

        ctx = GatewayRequestContext(
            request_id=item_id,
            model=model,
            identity=identity,
        )

        try:
            # Per-item rate limit
            estimated_tokens = count_tokens_for_request(req, model)
            rl_ok, _ = await check_and_increment_rate_limit(
                identity, model, estimated_tokens, item_id, redis,
            )
            if not rl_ok:
                await _record_failure(redis, batch_id, i, "Rate limit exceeded")
                failed += 1
                await _update_progress(redis, batch_id, completed, failed)
                continue

            # Per-item budget check
            budget_ok, budget_reason = await check_budget(identity)
            if not budget_ok:
                await _record_failure(redis, batch_id, i, budget_reason)
                failed += 1
                await _update_progress(redis, batch_id, completed, failed)
                continue

            # Per-item pre-call guardrail
            from middleware.guardrails import run_pre_call_guardrails
            guard_ok, guard_reason, _ = await run_pre_call_guardrails(req, identity)
            if not guard_ok:
                await _record_failure(redis, batch_id, i, f"Guardrail: {guard_reason}")
                failed += 1
                await _update_progress(redis, batch_id, completed, failed)
                continue

            # Route and call
            deployment = await select_deployment(model, identity, redis, config)
            if not deployment:
                await _record_failure(redis, batch_id, i, "No deployment available")
                failed += 1
                await _update_progress(redis, batch_id, completed, failed)
                continue

            ctx.provider = deployment.provider
            ctx.deployment_id = deployment.deployment_id

            provider = get_provider(deployment.provider)
            url = provider.get_complete_url(deployment)
            headers = provider.get_headers(deployment)
            transformed = provider.transform_request(req, deployment)
            transformed.pop("stream", None)

            async with deployment_busy_counter(deployment.deployment_id, redis):
                response = await provider.send_request(url, headers, transformed, timeout=120.0)

            if response.status_code == 200:
                result = provider.transform_response(response.json(), deployment)

                # Post-call guardrail
                from middleware.guardrails import run_post_call_guardrails
                guard_ok, guard_reason, _ = await run_post_call_guardrails(result, identity)
                if not guard_ok:
                    await _record_failure(redis, batch_id, i, f"Post-guardrail: {guard_reason}")
                    failed += 1
                    await _update_progress(redis, batch_id, completed, failed)
                    continue

                inp, out = extract_usage_from_response(result, deployment.provider)
                await record_spend(ctx, inp, out)
                await redis.lpush(
                    f"{BATCH_PREFIX}{batch_id}:results",
                    json.dumps({"index": i, "response": result, "status": "completed"}),
                )
                completed += 1
            else:
                await _record_failure(redis, batch_id, i, f"Provider returned {response.status_code}")
                failed += 1

        except Exception as e:
            await _record_failure(redis, batch_id, i, str(e))
            failed += 1

        await _update_progress(redis, batch_id, completed, failed)

    await redis.hset(f"{BATCH_PREFIX}{batch_id}", "status", "completed")


async def _record_failure(redis, batch_id: str, index: int, error: str) -> None:
    await redis.lpush(
        f"{BATCH_PREFIX}{batch_id}:results",
        json.dumps({"index": index, "error": error, "status": "failed"}),
    )


async def _update_progress(redis, batch_id: str, completed: int, failed: int) -> None:
    await redis.hset(f"{BATCH_PREFIX}{batch_id}", mapping={
        "completed": str(completed), "failed": str(failed),
    })
