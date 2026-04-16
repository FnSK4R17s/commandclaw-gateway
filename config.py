"""Configuration loader — YAML config + env var substitution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings

from schemas.common import Deployment

_ENV_PATTERN = re.compile(r"^os\.environ/(\w+)$")


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively substitute os.environ/VAR_NAME references in config values."""
    if isinstance(obj, str):
        m = _ENV_PATTERN.match(obj)
        if m:
            return os.environ.get(m.group(1), "")
        return obj
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


class Settings(BaseSettings):
    """Environment-sourced settings."""

    gateway_master_key: str = ""
    gateway_salt_key: str = ""
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    config_path: str = "config.yaml"
    port: int = 4000
    log_level: str = "INFO"

    # Observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""
    slack_webhook_url: str = ""

    # JWT/OIDC (v1.1)
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_jwks_uri: str = ""
    jwt_secret: str = ""

    # Background health checks (v2)
    enable_health_checks: bool = False
    health_check_interval: int = 60

    # Log archival (v1.2)
    log_archival_bucket: str = ""
    log_archival_prefix: str = "gateway-logs/"

    model_config = {"env_prefix": "", "case_sensitive": False}


class RouterSettings:
    def __init__(self, raw: dict[str, Any]):
        self.routing_strategy: str = raw.get("routing_strategy", "simple-shuffle")
        self.num_retries: int = raw.get("num_retries", 3)
        self.timeout: float = raw.get("timeout", 30.0)
        self.allowed_fails: int = raw.get("allowed_fails", 3)
        self.cooldown_time: int = raw.get("cooldown_time", 60)
        self.fallbacks: dict[str, list[str]] = raw.get("fallbacks", {})
        self.context_window_fallbacks: dict[str, list[str]] = raw.get("context_window_fallbacks", {})
        self.content_policy_fallbacks: dict[str, list[str]] = raw.get("content_policy_fallbacks", {})
        self.default_fallback: str | None = raw.get("default_fallback")
        self.custom_routing_plugin: str | None = raw.get("custom_routing_plugin")
        self.canary_config: dict[str, Any] = raw.get("canary_config", {})
        self.mirror_config: dict[str, Any] = raw.get("mirror_config", {})
        self.use_token_bucket: bool = raw.get("use_token_bucket", False)


class CacheSettings:
    def __init__(self, raw: dict[str, Any]):
        self.type: str = raw.get("type", "redis")  # redis | memory | none
        self.host: str = raw.get("host", "localhost")
        self.port: int = raw.get("port", 6379)
        self.db: int = raw.get("db", 0)
        self.default_ttl: int = raw.get("default_ttl", 86400)
        self.per_model_ttl: dict[str, int] = raw.get("per_model_ttl", {})
        self.supported_call_types: list[str] = raw.get("supported_call_types", ["chat_completion"])
        self.cache_scope: str = raw.get("cache_scope", "team")  # global | team | key
        self.semantic_enabled: bool = raw.get("semantic_enabled", False)
        self.semantic_threshold: float = raw.get("semantic_threshold", 0.92)
        self.semantic_model: str = raw.get("semantic_model", "text-embedding-3-small")


class GatewayConfig:
    """Parsed from config.yaml."""

    def __init__(self, raw: dict[str, Any]):
        self.deployments: list[Deployment] = self._parse_deployments(raw.get("model_list", []))
        self.router_settings = RouterSettings(raw.get("router_settings", {}))
        self.cache_settings = CacheSettings(raw.get("cache_params", {}))
        self.auth_strategy: str = raw.get("general_settings", {}).get("auth_strategy", "api_key")
        self.success_callbacks: list[str] = raw.get("litellm_settings", {}).get("success_callbacks", [])
        self.failure_callbacks: list[str] = raw.get("litellm_settings", {}).get("failure_callbacks", [])
        self.slack_alerting: bool = raw.get("litellm_settings", {}).get("slack_alerting", False)

        # Build model name -> deployments index
        self._model_index: dict[str, list[Deployment]] = {}
        for d in self.deployments:
            self._model_index.setdefault(d.model_name, []).append(d)
        # Sort each group by order
        for group in self._model_index.values():
            group.sort(key=lambda d: d.order)

    def get_deployments_for_model(self, model_name: str) -> list[Deployment]:
        return list(self._model_index.get(model_name, []))

    def get_all_model_names(self) -> list[str]:
        return sorted(self._model_index.keys())

    @staticmethod
    def _parse_deployments(model_list: list[dict[str, Any]]) -> list[Deployment]:
        deployments = []
        for i, entry in enumerate(model_list):
            model_name = entry.get("model_name", f"model-{i}")
            params = entry.get("litellm_params", {})
            info = entry.get("model_info", {})

            # Parse provider from model string: "openai/gpt-4o" -> provider="openai", provider_model="gpt-4o"
            raw_model = params.get("model", model_name)
            if "/" in raw_model:
                provider, provider_model = raw_model.split("/", 1)
            else:
                provider, provider_model = "openai", raw_model

            deployments.append(Deployment(
                deployment_id=f"{model_name}-{provider}-{i}",
                model_name=model_name,
                provider=provider,
                provider_model=provider_model,
                api_base=params.get("api_base", ""),
                api_key_env=params.get("api_key", ""),
                rpm=params.get("rpm"),
                tpm=params.get("tpm"),
                max_parallel_requests=params.get("max_parallel_requests"),
                order=params.get("order", 1),
                weight=params.get("weight", 1.0),
                region=info.get("region"),
                context_window=info.get("context_window"),
                model_info=info,
            ))
        return deployments


def load_pricing(path: str = "pricing.yaml") -> dict[str, dict[str, float]]:
    pricing_path = Path(path)
    if not pricing_path.exists():
        return {}
    with open(pricing_path) as f:
        return yaml.safe_load(f) or {}


# ── Module-level singletons ──

settings = Settings()

_gateway_config: GatewayConfig | None = None
_pricing_table: dict[str, dict[str, float]] = {}


def load_config() -> GatewayConfig:
    """Load and parse the gateway config from YAML."""
    global _gateway_config, _pricing_table

    config_path = Path(settings.config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    raw = _resolve_env_vars(raw)
    _gateway_config = GatewayConfig(raw)
    _pricing_table = load_pricing()
    return _gateway_config


def get_config() -> GatewayConfig:
    if _gateway_config is None:
        return load_config()
    return _gateway_config


def get_pricing_table() -> dict[str, dict[str, float]]:
    return _pricing_table
