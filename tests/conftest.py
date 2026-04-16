"""Shared test fixtures."""

from __future__ import annotations

import os
import sys

import pytest

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Set required env vars before any imports
os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-key")
os.environ.setdefault("GATEWAY_SALT_KEY", "dGVzdC1zYWx0LWtleS0xMjM0NTY3ODkwYWJjZGVm")  # base64 test key
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml"))


@pytest.fixture
def sample_openai_request():
    return {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ],
        "temperature": 0.7,
        "max_tokens": 100,
    }


@pytest.fixture
def sample_anthropic_request():
    return {
        "model": "claude-sonnet",
        "messages": [
            {"role": "user", "content": "Hello!"},
        ],
        "max_tokens": 100,
        "system": "You are a helpful assistant.",
    }


@pytest.fixture
def sample_openai_response():
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "gpt-4o",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello! How can I help you today?"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
        },
    }


@pytest.fixture
def sample_anthropic_response():
    return {
        "id": "msg_test123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello! How can I help?"}],
        "model": "claude-sonnet-4-20250514",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 20,
            "output_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
