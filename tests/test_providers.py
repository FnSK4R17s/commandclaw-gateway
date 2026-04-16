"""Provider adapter transform tests."""

import json

import pytest

from schemas.common import Deployment


@pytest.fixture
def openai_deployment():
    return Deployment(
        deployment_id="gpt4o-openai-0",
        model_name="gpt-4o",
        provider="openai",
        provider_model="gpt-4o-2024-08-06",
        api_base="https://api.openai.com/v1",
        api_key_env="test-key",
    )


@pytest.fixture
def anthropic_deployment():
    return Deployment(
        deployment_id="claude-anthropic-0",
        model_name="claude-sonnet",
        provider="anthropic",
        provider_model="claude-sonnet-4-20250514",
        api_key_env="test-key",
    )


class TestOpenAIProvider:
    def test_transform_request_sets_model(self, openai_deployment, sample_openai_request):
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
        result = provider.transform_request(sample_openai_request, openai_deployment)
        assert result["model"] == "gpt-4o-2024-08-06"

    def test_transform_request_strips_metadata(self, openai_deployment):
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
        req = {"model": "gpt-4o", "messages": [], "metadata": {"tags": ["test"]}}
        result = provider.transform_request(req, openai_deployment)
        assert "metadata" not in result

    def test_transform_response_passthrough(self, openai_deployment, sample_openai_response):
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
        result = provider.transform_response(sample_openai_response, openai_deployment)
        assert result == sample_openai_response

    def test_transform_stream_chunk(self, openai_deployment):
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()

        line = 'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}'
        result = provider.transform_stream_chunk(line, openai_deployment)
        assert result is not None
        assert result["choices"][0]["delta"]["content"] == "Hello"

    def test_transform_stream_done(self, openai_deployment):
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
        result = provider.transform_stream_chunk("data: [DONE]", openai_deployment)
        assert result == {"_done": True}

    def test_transform_stream_ignores_non_data(self, openai_deployment):
        from providers.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
        assert provider.transform_stream_chunk("", openai_deployment) is None
        assert provider.transform_stream_chunk(": keep-alive", openai_deployment) is None


class TestAnthropicProvider:
    def test_transform_request_extracts_system(self, anthropic_deployment, sample_openai_request):
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()
        result = provider.transform_request(sample_openai_request, anthropic_deployment)
        assert result.get("system") == "You are a helpful assistant."
        # System message should not be in messages list
        for msg in result["messages"]:
            assert msg["role"] != "system"

    def test_transform_request_sets_max_tokens_default(self, anthropic_deployment):
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()
        req = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "Hi"}]}
        result = provider.transform_request(req, anthropic_deployment)
        assert result["max_tokens"] == 4096

    def test_transform_request_converts_tools(self, anthropic_deployment):
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()
        req = {
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "Weather?"}],
            "tools": [{
                "type": "function",
                "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object"}},
            }],
        }
        result = provider.transform_request(req, anthropic_deployment)
        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "get_weather"
        assert "input_schema" in result["tools"][0]

    def test_transform_response_maps_content(self, anthropic_deployment, sample_anthropic_response):
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()
        result = provider.transform_response(sample_anthropic_response, anthropic_deployment)
        assert result["choices"][0]["message"]["content"] == "Hello! How can I help?"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert result["usage"]["prompt_tokens"] == 20

    def test_transform_response_maps_tool_use(self, anthropic_deployment):
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()
        resp = {
            "id": "msg_1",
            "content": [
                {"type": "text", "text": "Let me check the weather."},
                {"type": "tool_use", "id": "tc_1", "name": "get_weather", "input": {"city": "NYC"}},
            ],
            "model": "claude-sonnet-4-20250514",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 30, "output_tokens": 15},
        }
        result = provider.transform_response(resp, anthropic_deployment)
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        assert len(result["choices"][0]["message"]["tool_calls"]) == 1
        tc = result["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "NYC"}

    def test_transform_response_maps_stop_reasons(self, anthropic_deployment):
        from providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider()
        mappings = {
            "end_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "stop_sequence": "stop",
        }
        for anthropic_reason, openai_reason in mappings.items():
            resp = {
                "id": "msg_1", "content": [{"type": "text", "text": "test"}],
                "model": "claude-sonnet-4-20250514", "stop_reason": anthropic_reason,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
            result = provider.transform_response(resp, anthropic_deployment)
            assert result["choices"][0]["finish_reason"] == openai_reason
