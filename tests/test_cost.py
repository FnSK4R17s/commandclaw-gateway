"""Cost calculation tests."""

import os

# Set env vars before imports
os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml"))

from config import load_config
from infra.cost_calculator import calculate_cost
from middleware.cost_tracker import extract_usage_from_response

# Ensure pricing table is loaded
load_config()


class TestCostCalculation:
    def test_gpt4o_cost(self):
        cost = calculate_cost("gpt-4o", input_tokens=1000, output_tokens=500)
        # 1000/1000 * 0.0025 + 500/1000 * 0.010 = 0.0025 + 0.005 = 0.0075
        assert abs(cost - 0.0075) < 0.0001

    def test_zero_tokens_zero_cost(self):
        cost = calculate_cost("gpt-4o", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_unknown_model_uses_default(self):
        cost = calculate_cost("unknown-model-xyz", input_tokens=1000, output_tokens=1000)
        assert cost > 0  # Should use _default pricing

    def test_prefix_match(self):
        cost = calculate_cost("gpt-4o-2024-08-06", input_tokens=1000, output_tokens=500)
        # Should match gpt-4o pricing
        assert abs(cost - 0.0075) < 0.0001


class TestUsageExtraction:
    def test_openai_usage(self):
        response = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        }
        inp, out = extract_usage_from_response(response, "openai")
        assert inp == 100
        assert out == 50

    def test_anthropic_usage(self):
        response = {
            "usage": {"input_tokens": 100, "output_tokens": 50}
        }
        inp, out = extract_usage_from_response(response, "anthropic")
        assert inp == 100
        assert out == 50

    def test_missing_usage(self):
        inp, out = extract_usage_from_response({}, "openai")
        assert inp == 0
        assert out == 0
