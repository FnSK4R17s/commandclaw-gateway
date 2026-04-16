"""Token counting via tiktoken with fallback for non-OpenAI models."""

from __future__ import annotations

from typing import Any

import tiktoken

_encoder_cache: dict[str, tiktoken.Encoding] = {}

# Models that use specific tiktoken encodings
_MODEL_ENCODINGS: dict[str, str] = {
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-4": "cl100k_base",
    "gpt-3.5-turbo": "cl100k_base",
}


def _get_encoder(model: str) -> tiktoken.Encoding | None:
    # Try exact match first
    enc_name = _MODEL_ENCODINGS.get(model)
    if not enc_name:
        # Try prefix match (gpt-4o-2024-08-06 -> gpt-4o)
        for prefix, name in _MODEL_ENCODINGS.items():
            if model.startswith(prefix):
                enc_name = name
                break

    if not enc_name:
        return None

    if enc_name not in _encoder_cache:
        _encoder_cache[enc_name] = tiktoken.get_encoding(enc_name)
    return _encoder_cache[enc_name]


def count_tokens_text(text: str, model: str) -> int:
    encoder = _get_encoder(model)
    if encoder:
        return len(encoder.encode(text))
    # Fallback: rough estimate for non-tiktoken models
    return int(len(text.split()) * 1.3)


def count_tokens_messages(messages: list[dict[str, Any]], model: str) -> int:
    """Count tokens for a list of OpenAI-format chat messages."""
    encoder = _get_encoder(model)
    if encoder:
        # Per OpenAI's token counting guide
        tokens_per_message = 3  # <|im_start|>{role}\n ... <|im_end|>\n
        total = 0
        for msg in messages:
            total += tokens_per_message
            for key, value in msg.items():
                if isinstance(value, str):
                    total += len(encoder.encode(value))
                elif isinstance(value, list):
                    # Multimodal content blocks
                    for block in value:
                        if isinstance(block, dict) and "text" in block:
                            total += len(encoder.encode(block["text"]))
        total += 3  # priming tokens
        return total

    # Fallback
    text = " ".join(
        str(msg.get("content", "")) for msg in messages
    )
    return int(len(text.split()) * 1.3)


def count_tokens_for_request(request_data: dict[str, Any], model: str) -> int:
    """Estimate input tokens for a request dict."""
    messages = request_data.get("messages", [])
    if isinstance(messages, list):
        return count_tokens_messages(messages, model)
    return 0
