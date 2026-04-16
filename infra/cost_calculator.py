"""Per-request cost calculation from the pricing table."""

from __future__ import annotations

from config import get_pricing_table


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost for a request. Uses pricing.yaml lookup with _default fallback."""
    table = get_pricing_table()
    pricing = table.get(model)

    # Try prefix match (claude-sonnet-4-20250514 -> claude-sonnet-4)
    if not pricing:
        for name, p in table.items():
            if name != "_default" and model.startswith(name):
                pricing = p
                break

    if not pricing:
        pricing = table.get("_default", {"input": 0.005, "output": 0.015})

    input_cost = (input_tokens / 1000) * pricing.get("input", 0)
    output_cost = (output_tokens / 1000) * pricing.get("output", 0)
    return round(input_cost + output_cost, 8)


def get_model_pricing(model: str) -> dict[str, float] | None:
    table = get_pricing_table()
    pricing = table.get(model)
    if not pricing:
        for name, p in table.items():
            if name != "_default" and model.startswith(name):
                return p
    return pricing
