"""Retry logic — v1 flat retries, v2 per-exception RetryPolicy + AllowedFailsPolicy."""

from __future__ import annotations

import random
from email.utils import parsedate_to_datetime
from typing import Any


# ── v2: Per-exception retry policies ──

class RetryPolicy:
    """Controls retry count per error type."""
    def __init__(
        self,
        bad_request: int = 0,
        auth_error: int = 0,
        timeout: int = 4,
        rate_limit: int = 3,
        content_policy: int = 0,
        server_error: int = 2,
        default: int = 3,
    ):
        self._map = {
            400: bad_request,
            401: auth_error,
            403: auth_error,
            408: timeout,
            429: rate_limit,
            451: content_policy,
            500: server_error,
            502: server_error,
            503: server_error,
            504: timeout,
        }
        self.default = default

    def max_retries_for(self, status_code: int) -> int:
        return self._map.get(status_code, self.default)


class AllowedFailsPolicy:
    """Controls cooldown threshold per error type."""
    def __init__(
        self,
        bad_request: int = 1000,
        auth_error: int = 1,
        timeout: int = 5,
        rate_limit: int = 10,
        content_policy: int = 50,
        server_error: int = 3,
        default: int = 3,
    ):
        self._map = {
            400: bad_request,
            401: auth_error,
            403: auth_error,
            408: timeout,
            429: rate_limit,
            451: content_policy,
            500: server_error,
            502: server_error,
            503: server_error,
            504: timeout,
        }
        self.default = default

    def allowed_fails_for(self, status_code: int) -> int:
        return self._map.get(status_code, self.default)


# Default policies (v1 compatibility — flat values)
DEFAULT_RETRY_POLICY = RetryPolicy()
DEFAULT_FAILS_POLICY = AllowedFailsPolicy()


def compute_backoff(attempt: int, base: float = 1.0, max_wait: float = 60.0) -> float:
    """Exponential backoff with jitter."""
    return min(base * (2 ** attempt) + random.uniform(0, 1), max_wait)


def parse_retry_after(headers: dict[str, Any]) -> float | None:
    """Parse Retry-After header (integer seconds or HTTP-date)."""
    value = headers.get("retry-after") or headers.get("Retry-After")
    if not value:
        return None
    value = str(value).strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        import time
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404})


def should_retry(
    status_code: int,
    attempt: int,
    max_retries: int,
    retry_policy: RetryPolicy | None = None,
) -> bool:
    """Check if a request should be retried. Supports v2 per-exception policy."""
    if retry_policy:
        policy_max = retry_policy.max_retries_for(status_code)
        return attempt < policy_max
    # v1: flat check
    if attempt >= max_retries:
        return False
    if status_code in NON_RETRYABLE_STATUS_CODES:
        return False
    return status_code in RETRYABLE_STATUS_CODES


def is_context_window_error(status_code: int, response_body: dict[str, Any] | None) -> bool:
    if status_code != 400:
        return False
    if not response_body:
        return False
    error_msg = ""
    error = response_body.get("error", {})
    if isinstance(error, dict):
        error_msg = error.get("message", "").lower()
    elif isinstance(error, str):
        error_msg = error.lower()
    indicators = [
        "context length", "context_length", "maximum context",
        "token limit", "too many tokens", "max_tokens", "input too long",
    ]
    return any(i in error_msg for i in indicators)


def is_content_policy_error(status_code: int, response_body: dict[str, Any] | None) -> bool:
    """Detect content policy violations for content policy fallback (v2)."""
    if status_code not in (400, 451):
        return False
    if not response_body:
        return False
    error_msg = ""
    error = response_body.get("error", {})
    if isinstance(error, dict):
        error_msg = error.get("message", "").lower()
        error_code = error.get("code", "").lower()
        if "content_policy" in error_code or "content_filter" in error_code:
            return True
    indicators = ["content policy", "content filter", "blocked", "safety", "moderation"]
    return any(i in error_msg for i in indicators)
