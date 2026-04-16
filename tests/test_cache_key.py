"""Cache key correctness tests — the most important test file per PRD.

Verifies that cache keys include all output-affecting fields
and exclude all non-output-affecting fields.
"""

import pytest

from middleware.cache import build_cache_key


@pytest.fixture
def base_request():
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Hello"}],
        "temperature": 0.7,
        "max_tokens": 100,
    }


class TestCacheKeyInclusion:
    """Fields that MUST change the cache key when modified."""

    def test_model_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "model": "gpt-4o-mini"}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_messages_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "messages": [{"role": "user", "content": "Different"}]}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_temperature_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "temperature": 0.0}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_top_p_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "top_p": 0.5}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_max_tokens_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "max_tokens": 200}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_tools_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "tools": [{"type": "function", "function": {"name": "get_weather"}}]}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_response_format_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "response_format": {"type": "json_object"}}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_seed_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "seed": 42}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_stop_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "stop": ["END"]}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_frequency_penalty_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "frequency_penalty": 0.5}
        key2 = build_cache_key(modified)
        assert key1 != key2

    def test_presence_penalty_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "presence_penalty": 0.5}
        key2 = build_cache_key(modified)
        assert key1 != key2


class TestCacheKeyExclusion:
    """Fields that must NOT change the cache key."""

    def test_stream_does_not_change_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "stream": True}
        key2 = build_cache_key(modified)
        assert key1 == key2

    def test_user_does_not_change_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "user": "user-123"}
        key2 = build_cache_key(modified)
        assert key1 == key2

    def test_metadata_does_not_change_key(self, base_request):
        key1 = build_cache_key(base_request)
        modified = {**base_request, "metadata": {"tags": ["test"], "session": "abc"}}
        key2 = build_cache_key(modified)
        assert key1 == key2

    def test_different_users_same_content_same_key(self, base_request):
        req1 = {**base_request, "user": "alice"}
        req2 = {**base_request, "user": "bob"}
        assert build_cache_key(req1) == build_cache_key(req2)


class TestCacheKeyDeterminism:
    """Cache key must be deterministic."""

    def test_same_request_same_key(self, base_request):
        assert build_cache_key(base_request) == build_cache_key(base_request)

    def test_field_order_does_not_matter(self):
        req1 = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}], "temperature": 0.7}
        req2 = {"temperature": 0.7, "model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}
        assert build_cache_key(req1) == build_cache_key(req2)

    def test_namespace_changes_key(self, base_request):
        key1 = build_cache_key(base_request)
        key2 = build_cache_key(base_request, namespace="team-alpha")
        assert key1 != key2
