"""GET /v1/models — model enumeration."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import get_config

router = APIRouter(tags=["models"])


@router.get("/v1/models")
async def list_models(request: Request):
    config = get_config()
    model_names = config.get_all_model_names()

    models = []
    seen = set()
    for name in model_names:
        if name in seen:
            continue
        seen.add(name)

        deployments = config.get_deployments_for_model(name)
        context_window = None
        if deployments:
            context_window = deployments[0].context_window

        model_data = {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "commandclaw-gateway",
        }
        if context_window:
            model_data["context_window"] = context_window

        models.append(model_data)

    return JSONResponse(content={
        "object": "list",
        "data": models,
    })
