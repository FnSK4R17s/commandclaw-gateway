"""Guardrail chain — PII detection (Presidio), generic API, prompt injection.

v1.1: Presidio PII + generic guardrail API
v1.2: Prompt injection detection
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from schemas.common import IdentityContext

logger = logging.getLogger(__name__)


class GuardrailResult:
    def __init__(self, passed: bool, reason: str = "", redacted: dict | None = None,
                 guardrail_name: str = "", details: dict | None = None):
        self.passed = passed
        self.reason = reason
        self.redacted = redacted  # Redacted version of input/output if applicable
        self.guardrail_name = guardrail_name
        self.details = details or {}


# ── Presidio PII Detection/Redaction ──

_PII_PATTERNS = {
    "email": re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
    "phone": re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'),
    "ssn": re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    "credit_card": re.compile(r'\b(?:\d{4}[-\s]?){3}\d{4}\b'),
    "ip_address": re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
}


def _detect_pii_builtin(text: str) -> list[dict[str, Any]]:
    """Built-in PII detection using regex patterns (fallback when Presidio not installed)."""
    findings = []
    for pii_type, pattern in _PII_PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append({
                "type": pii_type,
                "start": match.start(),
                "end": match.end(),
                "text": match.group(),
            })
    return findings


def _redact_pii(text: str, findings: list[dict[str, Any]]) -> str:
    """Redact detected PII from text."""
    result = text
    for finding in sorted(findings, key=lambda f: f["start"], reverse=True):
        placeholder = f"[{finding['type'].upper()}_REDACTED]"
        result = result[:finding["start"]] + placeholder + result[finding["end"]:]
    return result


async def _run_presidio(text: str, mode: str = "detect") -> GuardrailResult:
    """Run PII detection. Uses built-in patterns; can be swapped for Presidio analyzer."""
    try:
        # Try Presidio first
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            analyzer = AnalyzerEngine()
            results = analyzer.analyze(text=text, language="en")
            if results:
                findings = [{"type": r.entity_type, "start": r.start, "end": r.end,
                            "text": text[r.start:r.end], "score": r.score}
                           for r in results if r.score > 0.7]
                if findings:
                    if mode == "redact":
                        anonymizer = AnonymizerEngine()
                        anon = anonymizer.anonymize(text=text, analyzer_results=results)
                        return GuardrailResult(
                            passed=True, guardrail_name="presidio",
                            redacted={"text": anon.text},
                            details={"findings": findings},
                        )
                    return GuardrailResult(
                        passed=False,
                        reason=f"PII detected: {', '.join(set(f['type'] for f in findings))}",
                        guardrail_name="presidio",
                        details={"findings": findings},
                    )
            return GuardrailResult(passed=True, guardrail_name="presidio")
        except ImportError:
            pass

        # Fallback to built-in patterns
        findings = _detect_pii_builtin(text)
        if findings:
            if mode == "redact":
                redacted_text = _redact_pii(text, findings)
                return GuardrailResult(
                    passed=True, guardrail_name="pii_builtin",
                    redacted={"text": redacted_text},
                    details={"findings": findings},
                )
            return GuardrailResult(
                passed=False,
                reason=f"PII detected: {', '.join(set(f['type'] for f in findings))}",
                guardrail_name="pii_builtin",
                details={"findings": findings},
            )
        return GuardrailResult(passed=True, guardrail_name="pii_builtin")
    except Exception:
        logger.exception("PII detection failed")
        return GuardrailResult(passed=True, guardrail_name="pii_error")


# ── Generic Guardrail API ──

async def _run_generic_guardrail(
    content: str,
    guardrail_url: str,
    stage: str = "pre",
) -> GuardrailResult:
    """Call an external guardrail API endpoint."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(guardrail_url, json={
                "content": content,
                "stage": stage,
            })
            if resp.status_code != 200:
                logger.warning("Guardrail API returned %d", resp.status_code)
                return GuardrailResult(passed=True, guardrail_name="generic_api")

            result = resp.json()
            passed = result.get("passed", True)
            return GuardrailResult(
                passed=passed,
                reason=result.get("reason", "Blocked by guardrail"),
                guardrail_name="generic_api",
                details=result.get("details", {}),
            )
    except Exception:
        logger.exception("Generic guardrail API call failed")
        return GuardrailResult(passed=True, guardrail_name="generic_api_error")


# ── Prompt Injection Detection (v1.2) ──

_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?previous\s+instructions', re.I),
    re.compile(r'ignore\s+(all\s+)?(above|prior)\s+(instructions|prompts|rules)', re.I),
    re.compile(r'you\s+are\s+now\s+(a|an)', re.I),
    re.compile(r'disregard\s+(all\s+)?(previous|prior|above)', re.I),
    re.compile(r'forget\s+(all\s+)?(previous|prior|above)', re.I),
    re.compile(r'new\s+instructions?\s*:', re.I),
    re.compile(r'system\s*:\s*you\s+are', re.I),
    re.compile(r'<\|im_start\|>system', re.I),
    re.compile(r'\[INST\].*\[/INST\]', re.I),
]


def _detect_injection(text: str) -> list[str]:
    """Detect common prompt injection patterns."""
    detected = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            detected.append(pattern.pattern)
    return detected


# ── Main Guardrail Dispatch ──

# Guardrail policies: name -> config
GUARDRAIL_POLICIES: dict[str, dict[str, Any]] = {
    "pii_detect": {"type": "presidio", "mode": "detect"},
    "pii_redact": {"type": "presidio", "mode": "redact"},
    "injection_detect": {"type": "injection"},
    "strict": {"type": "all", "mode": "detect"},  # All guardrails
}


def _extract_text(data: dict[str, Any]) -> str:
    """Extract text content from request/response data for guardrail analysis."""
    messages = data.get("messages", [])
    parts = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("text"):
                    parts.append(block["text"])
    return " ".join(parts)


async def run_pre_call_guardrails(
    request_data: dict[str, Any],
    identity: IdentityContext,
) -> tuple[bool, str, list[dict]]:
    """Run pre-call guardrails. Returns (allowed, reason, guardrail_results)."""
    policy_name = identity.guardrail_policy
    if not policy_name:
        return True, "", []

    policy = GUARDRAIL_POLICIES.get(policy_name, {})
    guardrail_type = policy.get("type", "")
    mode = policy.get("mode", "detect")
    text = _extract_text(request_data)
    results = []

    if guardrail_type in ("presidio", "all"):
        result = await _run_presidio(text, mode)
        results.append({"guardrail": result.guardrail_name, "passed": result.passed,
                        "details": result.details})
        if not result.passed:
            return False, result.reason, results
        if result.redacted and mode == "redact":
            # Mutate the request with redacted content
            _apply_redaction(request_data, result.redacted["text"])

    if guardrail_type in ("injection", "all"):
        detected = _detect_injection(text)
        passed = len(detected) == 0
        results.append({"guardrail": "injection_detect", "passed": passed,
                        "details": {"patterns": detected}})
        if not passed:
            return False, "Potential prompt injection detected", results

    # Generic guardrail API
    generic_url = policy.get("api_url")
    if generic_url:
        result = await _run_generic_guardrail(text, generic_url, "pre")
        results.append({"guardrail": result.guardrail_name, "passed": result.passed,
                        "details": result.details})
        if not result.passed:
            return False, result.reason, results

    return True, "", results


async def run_post_call_guardrails(
    response_data: dict[str, Any],
    identity: IdentityContext,
) -> tuple[bool, str, list[dict]]:
    """Run post-call guardrails on response."""
    policy_name = identity.guardrail_policy
    if not policy_name:
        return True, "", []

    policy = GUARDRAIL_POLICIES.get(policy_name, {})
    guardrail_type = policy.get("type", "")
    mode = policy.get("mode", "detect")
    results = []

    # Extract text from response
    choices = response_data.get("choices", [])
    text_parts = []
    for choice in choices:
        msg = choice.get("message", {})
        content = msg.get("content", "")
        if content:
            text_parts.append(content)
    text = " ".join(text_parts)

    if not text:
        return True, "", []

    if guardrail_type in ("presidio", "all"):
        result = await _run_presidio(text, mode)
        results.append({"guardrail": result.guardrail_name, "passed": result.passed,
                        "details": result.details})
        if not result.passed:
            return False, result.reason, results

    generic_url = policy.get("api_url")
    if generic_url:
        result = await _run_generic_guardrail(text, generic_url, "post")
        results.append({"guardrail": result.guardrail_name, "passed": result.passed,
                        "details": result.details})
        if not result.passed:
            return False, result.reason, results

    return True, "", results


def _apply_redaction(request_data: dict[str, Any], redacted_text: str) -> None:
    """Replace the last user message content with redacted version."""
    messages = request_data.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            msg["content"] = redacted_text
            break
