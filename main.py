"""commandclaw-gateway — LLM gateway for the CommandClaw ecosystem.

Full v1–v2 feature set.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from auth.budgets import start_budget_scheduler
from auth.middleware import AuthMiddleware
from config import load_config, settings
from infra.redis_client import close_redis, get_redis
from routes.audit import router as audit_router
from routes.batches import router as batches_router
from routes.chat import router as chat_router
from routes.embeddings import router as embeddings_router
from routes.global_spend import router as global_spend_router
from routes.health import router as health_router
from routes.keys import router as keys_router
from routes.messages import router as messages_router
from routes.models import router as models_router
from routes.orgs import router as orgs_router
from routes.responses import router as responses_router
from routes.spend import router as spend_router
from routes.teams import router as teams_router
from routes.users import router as users_router

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    stream=sys.stdout,
)
logger = logging.getLogger("commandclaw-gateway")

_budget_task = None
_health_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _budget_task, _health_task
    logger.info("Starting commandclaw-gateway")

    config = load_config()
    logger.info(
        "Config loaded: %d models, strategy=%s, cache=%s, auth=%s",
        len(config.get_all_model_names()),
        config.router_settings.routing_strategy,
        config.cache_settings.type,
        config.auth_strategy,
    )

    redis = await get_redis()
    await redis.ping()
    logger.info("Redis connected at %s:%s", settings.redis_host, settings.redis_port)

    _budget_task = start_budget_scheduler()

    # Background health checks (v2)
    if settings.enable_health_checks:
        from observability.health import start_health_checks
        _health_task = start_health_checks()
        logger.info("Background health checks enabled (interval=%ds)", settings.health_check_interval)

    yield

    if _budget_task:
        _budget_task.cancel()
    if _health_task:
        _health_task.cancel()
    await close_redis()
    logger.info("Gateway shut down")


app = FastAPI(
    title="commandclaw-gateway",
    version="2.0.0",
    description="LLM gateway for the CommandClaw ecosystem — v1 through v2",
    lifespan=lifespan,
)

app.add_middleware(AuthMiddleware)

# Core LLM routes
app.include_router(health_router)
app.include_router(chat_router)
app.include_router(messages_router)
app.include_router(embeddings_router)
app.include_router(models_router)

# v2 endpoints
app.include_router(batches_router)
app.include_router(responses_router)

# Admin routes
app.include_router(keys_router)
app.include_router(users_router)
app.include_router(teams_router)       # v1.1
app.include_router(orgs_router)        # v1.2
app.include_router(audit_router)       # v1.2

# Spend + cache management
app.include_router(spend_router)
app.include_router(global_spend_router)  # v1.2


@app.get("/")
async def root():
    return {"service": "commandclaw-gateway", "version": "2.0.0"}
