"""Fallback chain — standard, context window, content policy (v2)."""

from __future__ import annotations

from typing import Literal

from config import GatewayConfig


class FallbackChain:
    def __init__(self, primary_model: str, config: GatewayConfig):
        self.primary_model = primary_model
        self.standard_fallbacks = config.router_settings.fallbacks.get(primary_model, [])
        self.context_window_fallbacks = config.router_settings.context_window_fallbacks.get(primary_model, [])
        self.content_policy_fallbacks = getattr(config.router_settings, "content_policy_fallbacks", {}).get(primary_model, [])
        self.default_fallback = config.router_settings.default_fallback

    def get_fallback_models(
        self,
        error_type: Literal["standard", "context_window", "content_policy"] = "standard",
    ) -> list[str]:
        if error_type == "context_window" and self.context_window_fallbacks:
            models = list(self.context_window_fallbacks)
        elif error_type == "content_policy" and self.content_policy_fallbacks:
            models = list(self.content_policy_fallbacks)
        else:
            models = list(self.standard_fallbacks)

        if self.default_fallback and self.default_fallback not in models:
            models.append(self.default_fallback)
        return models

    def has_fallbacks(self) -> bool:
        return bool(self.standard_fallbacks or self.context_window_fallbacks
                    or self.content_policy_fallbacks or self.default_fallback)
