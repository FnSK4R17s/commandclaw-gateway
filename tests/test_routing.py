"""Routing tests."""


from routing.fallbacks import FallbackChain
from routing.retries import compute_backoff, is_context_window_error, parse_retry_after, should_retry


class TestRetries:
    def test_should_retry_on_429(self):
        assert should_retry(429, 0, 3) is True

    def test_should_retry_on_500(self):
        assert should_retry(500, 0, 3) is True

    def test_should_not_retry_on_400(self):
        assert should_retry(400, 0, 3) is False

    def test_should_not_retry_on_401(self):
        assert should_retry(401, 0, 3) is False

    def test_should_not_retry_when_max_reached(self):
        assert should_retry(429, 3, 3) is False

    def test_compute_backoff_increases(self):
        b0 = compute_backoff(0, base=1.0)
        b1 = compute_backoff(1, base=1.0)
        b2 = compute_backoff(2, base=1.0)
        # Backoff should generally increase (with jitter)
        assert b1 > b0 * 0.5  # Allow for jitter
        assert b2 > b1 * 0.5

    def test_compute_backoff_respects_max(self):
        result = compute_backoff(20, base=1.0, max_wait=10.0)
        assert result <= 11.0  # max_wait + jitter

    def test_parse_retry_after_integer(self):
        assert parse_retry_after({"retry-after": "30"}) == 30.0

    def test_parse_retry_after_missing(self):
        assert parse_retry_after({}) is None


class TestContextWindowError:
    def test_detects_context_length_error(self):
        body = {"error": {"message": "This model's maximum context length is 4096 tokens"}}
        assert is_context_window_error(400, body) is True

    def test_detects_token_limit(self):
        body = {"error": {"message": "Request exceeds the token limit for this model"}}
        assert is_context_window_error(400, body) is True

    def test_rejects_non_400(self):
        body = {"error": {"message": "context length exceeded"}}
        assert is_context_window_error(500, body) is False

    def test_rejects_unrelated_400(self):
        body = {"error": {"message": "Invalid model name"}}
        assert is_context_window_error(400, body) is False


class TestFallbackChain:
    def _make_config(self):
        class MockRouterSettings:
            fallbacks = {"gpt-4o": ["claude-sonnet", "gpt-4o-mini"]}
            context_window_fallbacks = {"gpt-4o": ["gpt-4o-128k"]}
            default_fallback = "claude-haiku"

        class MockConfig:
            router_settings = MockRouterSettings()

        return MockConfig()

    def test_standard_fallbacks(self):
        config = self._make_config()
        chain = FallbackChain("gpt-4o", config)
        models = chain.get_fallback_models("standard")
        assert models == ["claude-sonnet", "gpt-4o-mini", "claude-haiku"]

    def test_context_window_fallbacks(self):
        config = self._make_config()
        chain = FallbackChain("gpt-4o", config)
        models = chain.get_fallback_models("context_window")
        assert models == ["gpt-4o-128k", "claude-haiku"]

    def test_no_fallbacks(self):
        config = self._make_config()
        chain = FallbackChain("unknown-model", config)
        models = chain.get_fallback_models("standard")
        assert models == ["claude-haiku"]  # Only default

    def test_has_fallbacks(self):
        config = self._make_config()
        assert FallbackChain("gpt-4o", config).has_fallbacks() is True

    def test_default_fallback_not_duplicated(self):
        config = self._make_config()
        config.router_settings.fallbacks["gpt-4o"].append("claude-haiku")
        chain = FallbackChain("gpt-4o", config)
        models = chain.get_fallback_models("standard")
        # claude-haiku should appear only once
        assert models.count("claude-haiku") == 1
