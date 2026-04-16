# PRD Review Findings (2026-04-16)

Scope: current `commandclaw-gateway` working tree reviewed against [COMMANDCLAW_GATEWAY_PRD.md](./COMMANDCLAW_GATEWAY_PRD.md), especially the architecture and pipeline requirements in lines 21-39.

## Verification

- Test run: `cd /apps/commandclaw-gateway && ./.venv/bin/python -m pytest -q -o cache_dir=/tmp/pytest-commandclaw-gateway-all`
- Result: `82 passed in 0.38s`
- Optional static lint attempt: `./.venv/bin/python -m ruff check .`
- Lint result: not executed because `ruff` is not installed in the project virtualenv (`No module named ruff`)
- Interpretation: the current test suite is green, but it does not cover several PRD-critical behaviors. The findings below are implementation/coverage mismatches, not failures already caught by tests.

## Findings

### 1. Admin auth flow does not match the PRD identity model

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:21-25`
- Code: `auth/middleware.py:57-69`, `auth/middleware.py:91-100`
- Severity: High

The PRD says every request should carry identity context that downstream systems read, and that v1.1 adds RBAC/Cerbos without changing the middleware chain. The implementation instead hard-requires the master key for every non-`GET` admin route before virtual-key or JWT auth runs. That makes `proxy_admin` and `team_admin` identities ineffective for write operations and turns RBAC into mostly dead code for admin mutations.

Impact:

- Admin writes do not follow the PRD's identity-based authorization model.
- The implementation behaves like "master key only" rather than "authenticate then authorize."

### 2. `/v1/responses` bypasses the shared request pipeline

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:21-39`
- Code: `routes/responses.py:108-135`
- Severity: High

The PRD describes a shared middleware pipeline beneath the API layer. `/v1/responses` translates the body and then calls `_route_and_call()` directly. It skips rate limiting, budget checks, cache lookup/write, pre/post guardrails, callback hooks, and cache-hit accounting.

Impact:

- Responses API requests can be admitted even when the gateway should say no.
- Behavior diverges from `/v1/chat/completions` and `/v1/messages`.

### 3. `/v1/batches` bypasses per-request enforcement for each batch item

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:21-39`
- Code: `routes/batches.py:33-59`, `routes/batches.py:82-132`
- Severity: High

Batch items are processed in the background and sent directly through provider selection and dispatch. The batch path does not apply per-item rate-limit checks, budget admission checks, or guardrails before each provider call.

Impact:

- A single accepted batch can overspend budgets or exceed rate limits through its child requests.
- Batch behavior is materially different from the rest of the gateway pipeline.

### 4. `/v1/embeddings` skips budget and rate-limit admission

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:11`, `docs/COMMANDCLAW_GATEWAY_PRD.md:21-27`
- Code: `routes/embeddings.py:23-77`
- Severity: High

Embeddings requests go straight from auth to deployment selection and provider dispatch. There is no call to `check_and_increment_rate_limit()` or `check_budget()`.

Impact:

- Embeddings traffic can proceed after the gateway should reject it for exhausted quota/budget.
- The core "say no when a budget is exhausted or a rate limit is hit" contract is not consistently enforced.

### 5. Cache isolation does not honor configured `cache_scope`

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:29`
- Code: `routes/chat.py:96-101`, `routes/messages.py:218-223`, `middleware/cache.py:25-43`
- Severity: High

`build_cache_key()` supports `cache_scope` plus `team_id` and `key_id`, but both chat and messages call it with only `namespace`. That means the routes silently fall back to the default `"global"` scope even if config is set to `"team"` or `"key"`.

Impact:

- Multi-tenant cache isolation does not behave as advertised.
- With the documented safe default of team scope, requests can still collapse onto global cache keys.

### 6. Post-call guardrails are not enforced before return/cache/spend

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:39`
- Code: `routes/chat.py:174-185`, `routes/messages.py:248-263`
- Severity: High

In chat, `run_post_call_guardrails()` is called but its result is ignored. In messages, non-streaming responses never run post-call guardrails at all. Both paths proceed to spend logging and cache writes regardless.

Impact:

- Responses that fail post-call policy can still be returned to the caller.
- Rejected responses can still be cached and billed.

### 7. Anthropic usage extraction is inconsistent with the gateway's normalized response format

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:37`
- Code: `middleware/cost_tracker.py:22-32`
- Severity: High

The gateway normalizes provider responses to the OpenAI-style internal schema before cost tracking in most routes. `extract_usage_from_response()` still switches on `provider == "anthropic"` and reads `input_tokens`/`output_tokens` instead of the normalized `prompt_tokens`/`completion_tokens`.

Impact:

- Anthropic calls in normalized paths can be recorded as zero tokens and zero cost.
- Spend data and budget enforcement become inaccurate for Anthropic traffic.

### 8. Team/org spend counters are never incremented by `record_spend()`

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:37`, `docs/COMMANDCLAW_GATEWAY_PRD.md:58-67`
- Code: `middleware/cost_tracker.py:47-50`
- Severity: High

`auth/budgets.increment_spend()` supports key, user, team, and org counters, but `record_spend()` only passes `key_id` and `user_id`.

Impact:

- Team and org spend totals remain stale.
- Hierarchical spend accumulation and higher-level budget reporting do not match the PRD.

### 9. Anthropic streaming is not a first-class streaming path

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:23`, `docs/COMMANDCLAW_GATEWAY_PRD.md:29`, `docs/COMMANDCLAW_GATEWAY_PRD.md:242`
- Code: `routes/messages.py:265-299`
- Severity: Medium

The PRD says both API families are first-class and calls out streaming correctness as the most bug-prone area. The current `/v1/messages` streaming path does not proxy typed provider events incrementally. It waits for `_route_and_call()` to finish, converts the complete response, and then emits synthetic Anthropic events.

Impact:

- TTFT and backpressure behavior differ from true streaming.
- Provider-side event semantics and streaming errors are flattened away.

### 10. RBAC permission descriptors are internally inconsistent

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:25`
- Code: `auth/rbac.py:19-39`, `auth/rbac.py:54-59`
- Severity: Medium

`ENDPOINT_PERMISSIONS` mixes 2-tuples and 3-tuples, but `check_rbac()` unpacks each key as `(req_method, path_prefix)`. Today this is partly masked by the master-key short-circuit in admin auth. Once admin auth is brought in line with the PRD, routes like `POST /key/{id}/regenerate` and `POST /team/{id}/member` will raise instead of returning allow/deny.

Impact:

- RBAC implementation is not safe to enable as the main admin authorization path.
- The current green test suite is not exercising the full permission map.

### 11. Anthropic tool-call streams drop argument deltas

- PRD reference: `docs/COMMANDCLAW_GATEWAY_PRD.md:23`, `docs/COMMANDCLAW_GATEWAY_PRD.md:242`
- Code: `providers/anthropic_provider.py:244-255`, `providers/anthropic_provider.py:258-283`
- Severity: Medium

The adapter emits a `tool_calls` stub when a `tool_use` block starts, but when Anthropic streams the corresponding `input_json_delta`, the gateway returns `{"content": null}` instead of appending the JSON fragment into `function.arguments`.

Impact:

- OpenAI-style streaming clients can see tool-call name/id but never receive the streamed arguments.
- Tool-call streaming behavior is incomplete even if the broader synthetic `/v1/messages` stream path is left unchanged.

## Static Analysis Carry-Forward

This section captures the earlier file-level static analysis findings directly, even where they overlap with the broader PRD review above.

### Earlier static-analysis findings

1. `auth/rbac.py:19-39`, `auth/rbac.py:54-59`
   `ENDPOINT_PERMISSIONS` uses mixed 2-tuple and 3-tuple keys, but `check_rbac()` always unpacks two values. This will raise on routes such as `POST /key/{id}/regenerate` and `POST /team/{id}/member` when RBAC is exercised.

2. `middleware/cost_tracker.py:22-32`
   `extract_usage_from_response()` reads Anthropic-native `input_tokens`/`output_tokens` even after most gateway paths normalize provider responses to OpenAI-style `prompt_tokens`/`completion_tokens`.

3. `middleware/cost_tracker.py:47-50`
   `record_spend()` increments only key/user totals and omits `team_id` and `org_id`, so team/org spend does not track with the hierarchical budget model.

4. `routes/chat.py:96-101`, `routes/messages.py:218-223`, `middleware/cache.py:25-43`
   Cache key generation ignores configured `cache_scope` and tenant identity, so the effective scope falls back to global.

5. `routes/chat.py:174-185`
   `run_post_call_guardrails()` is called but the result is unused; the response can still be billed, cached, and returned after policy failure.

6. `providers/anthropic_provider.py:244-255`
   `input_json_delta` events are dropped instead of being surfaced as incremental `tool_calls[*].function.arguments` deltas.

## Summary

The most important mismatch is architectural: the PRD describes a single gateway pipeline that enforces identity, limits, caching, guardrails, routing, and spend consistently across endpoints. The current implementation only does that partially for chat/messages, and even those paths still have gaps around cache isolation, post-call guardrails, and Anthropic accounting.

The test suite passing cleanly is useful signal, but it currently validates happy-path transforms and helper behavior more than the PRD's end-to-end guarantees.
