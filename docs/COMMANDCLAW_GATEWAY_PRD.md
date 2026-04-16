# commandclaw-gateway

> A standalone LLM gateway that gives every agent in the CommandClaw ecosystem a virtual key, a budget, and a route to any provider — without importing a single line of LiteLLM.

This is an idea file. It is meant to be shared with an LLM coding agent that will build out the implementation. The architecture is concrete enough to start writing code, abstract enough to leave room for engineering judgment on the details. The phased milestones (v1 through v2) define what ships when — the agent should implement them in order.

## The core idea

Most LLM gateways today are monoliths. LiteLLM started as a simple proxy — "one interface, every LLM" — and grew into a 400+ feature platform that now includes agent orchestration (A2A), tool servers (MCP), RAG pipelines, file management, fine-tuning, evaluations, audio transcription, image generation, and an admin dashboard. It imports 100+ provider modules at startup, requires its own PostgreSQL instance, and carries a vulnerability surface that grows with every release.

CommandClaw doesn't need a platform. It already has one. commandclaw-mcp handles tools. commandclaw-observe handles observability infrastructure. The agent runtime handles orchestration. What's missing is the piece in the middle: the thing that sits between every agent and every LLM provider, that holds the provider credentials so agents don't have to, that tracks every token and every dollar, and that says "no" when a budget is exhausted or a rate limit is hit.

**The key insight: the gateway is an infrastructure service, not a product.** It does one thing — route LLM traffic — and it does it within the CommandClaw ecosystem, not as a standalone platform. Every feature that doesn't serve LLM routing belongs in another service. This constraint is what keeps the codebase at ~3,000 lines instead of ~300,000.

The second insight is about identity. LiteLLM's virtual key system is the best idea in the entire project — the notion that applications never see real provider credentials, that every request carries a scoped virtual key with a budget, a rate limit, and a model allowlist. commandclaw-mcp already proved this pattern works with phantom tokens. The gateway applies the same architecture to a different resource type: LLM calls instead of tool calls.

The third insight is about enterprise readiness as an architectural property, not a feature flag. The v1 schema includes `team_id` and `org_id` columns even though teams and orgs don't ship until v1.1/v1.2. The middleware chain has a pluggable auth slot even though JWT doesn't ship until v1.1. The rate limiter's Redis key structure includes dimension placeholders even though multi-dimensional enforcement doesn't ship until v1.1. **Building for enterprise means designing the data model and extension points on day one, then filling them in over four milestones.**

## Architecture

The gateway is a FastAPI service that sits between CommandClaw agents and LLM providers. Every request passes through a middleware pipeline before reaching a provider adapter.

**1. API Layer.** Two endpoint families that share everything beneath them. `POST /v1/chat/completions` speaks the OpenAI wire format — this is what LangChain's `ChatOpenAI(base_url=...)` hits. `POST /v1/messages` speaks the Anthropic Messages format — content blocks, typed SSE events, system as a top-level field. Both formats are first-class citizens, not one-translates-to-the-other. Supporting endpoints: `GET /v1/models`, `POST /v1/embeddings`, `POST /v1/messages/count_tokens`, health probes, Prometheus metrics. Batch inference (`/v1/batches`) and OpenAI Responses API (`/v1/responses`) arrive in v2.

**2. Auth Middleware.** Every request must present a virtual key. The middleware validates the key, loads its constraints (budget, rate limits, model allowlist, team, org), and attaches the identity context to the request. This is the gateway's core — every downstream system (routing, cost tracking, rate limiting, caching, observability) reads from this identity context. v1 uses API key auth. v1.1 adds JWT/OIDC as a pluggable alternative without changing the middleware chain. The gateway runs its own Cerbos instance for RBAC — no shared dependency with commandclaw-mcp.

**3. Rate Limiter.** Redis-backed sliding windows enforced hierarchically. A request must pass ALL applicable limits: per-key, per-key-per-model, per-user, per-team, per-team-per-model, per-org, per-model-global. v1 implements per-key RPM/TPM. v1.1 adds all dimensions plus daily/monthly quotas, upstream provider rate limit awareness, and a dynamic adjustment API. v2 adds token bucket (burst allowance), priority tiers, and fair queuing. The v1 Redis key structure uses dimension placeholders (`key:{key_id}:model:{model}:rpm`) so v1.1 is a config change, not a rewrite.

**4. Cache.** Redis exact-match cache with correct key composition (hash of model + messages + temperature + tools + response_format + seed, excluding stream/user/metadata). Streaming responses are buffered internally while chunks forward to the client; the complete response is written to cache only after successful completion. Cache hits count against RPM (consumed gateway capacity) but not against TPM or budget (no provider cost). Per-model TTL because different models have different staleness profiles. v1.1 adds multi-tenant cache isolation (`cache_scope: global | team | key`). v1.2 adds Redis semantic cache for paraphrased duplicates at enterprise chatbot scale.

**5. Router.** A filter pipeline: remove cooldown deployments, apply region constraint, validate context window, then apply the selected routing strategy. v1 ships with `simple-shuffle` (weighted random) and `least-busy` (fewest concurrent requests). v1.1 adds a custom routing plugin interface — enterprise writes a Python class implementing `select_deployment(deployments, context) -> deployment`, registers it in config, and the gateway loads it. v1.1 also adds region-based routing (GDPR/data residency) and traffic splitting (canary rollouts). v2 adds latency-based and cost-based routing.

**6. Reliability Engine.** Fallback chains, retries with exponential backoff and jitter, `Retry-After` header parsing, and a cooldown system that pauses failing deployments after a configurable failure threshold. Three fallback types: standard (any error), context window (auto-route to larger model), and content policy (auto-route to different provider). v2 adds per-exception retry and cooldown policies — different thresholds for 429 vs 500 vs timeout.

**7. Provider Adapters.** Each provider is an ABC subclass with four methods: `validate_environment()`, `get_complete_url()`, `transform_request()`, `transform_response()`. OpenAI-compatible providers (Groq, DeepSeek, Ollama) use the baseline adapter with zero custom code. Only providers with non-OpenAI wire formats need custom adapters: Anthropic (content blocks, system-as-field), Bedrock (SigV4 signing), Vertex AI (Google auth). Five adapters cover day-one needs. Adding an OpenAI-compatible provider takes 2 hours.

**8. Cost Tracker.** Token counting via tiktoken (with fallback for non-OpenAI models), per-request cost calculation from a YAML pricing table, per-key and per-user spend accumulation in Redis, hierarchical budget enforcement (org > team > user > key), and a budget reset scheduler. Custom spend tags (`metadata.tags`) answer "how much did the summarization pipeline cost vs. the chat pipeline?" Spend logs with metadata provide financial audit records. The gateway owns all spend data — Langfuse traces are observability artifacts, not financial records.

**9. Guardrail Chain.** Pre-call and post-call hook points in the middleware chain. v1 builds the framework (two hooks + config). v1.1 wires in Presidio for PII detection/redaction and a generic guardrail API that lets enterprise plug in their own guardrail service. Per-key guardrail assignment means different agents get different policies. Guardrail execution traces (pass/fail) feed into Langfuse for compliance audit.

**10. Observability.** Prometheus metrics (~15 counters, histograms, gauges covering spend, tokens, latency, TTFT, request counts, errors, cache hit rate, deployment health). Langfuse tracing via `@observe` decorator and success/failure callbacks. Structured JSON logging. Slack alerts for budget exhaustion, deployment outages, and slow responses. All metrics carry team/org/key/model label dimensions from v1 — even if not grouped by team/org until v1.1.

## The virtual key model

The virtual key is the gateway's identity primitive. Every other feature depends on it.

A virtual key (`sk-[alphanumeric]`) maps to a user, optionally a team, optionally an org. It carries constraints:

- **Model allowlist** — which models this key can access
- **Budget** — max spend in USD, with a reset duration (daily, monthly, custom)
- **Rate limits** — RPM and TPM, optionally per-model
- **Max parallel requests** — concurrency cap
- **Guardrail policy** — which guardrails run on this key's requests
- **Region constraint** — which deployment regions are allowed

The key-to-provider-credential mapping is one-to-many: a single virtual key can route to OpenAI, Anthropic, or Bedrock depending on the model requested. Provider credentials are encrypted at rest with a salt key. If a virtual key is compromised, it is blocked instantly without rotating provider secrets.

Budget enforcement is hierarchical:

```
Organization budget ($10,000/month)
  └── Team budget ($3,000/month)
       └── User budget ($500/month)
            └── Key budget ($100/day)
```

A request is blocked if ANY level in the chain is exhausted. The v1 schema includes all four levels even though only key and user are enforced in v1 — teams activate in v1.1, orgs in v1.2.

## Operations

### 1. Request Flow (Happy Path)

1. Agent sends `POST /v1/chat/completions` with virtual key in `Authorization` header
2. Auth middleware validates key, loads identity context (user, team, org, constraints)
3. Rate limiter checks all applicable limits (per-key RPM/TPM, per-team, per-model)
4. Cache checks for exact match on request hash — if hit, return cached response (skip steps 5-8)
5. Guardrail pre-call hook runs (PII detection, injection check) — blocks with 400 if violation
6. Router selects deployment: filter cooldowns, filter region, apply strategy (shuffle/least-busy/custom)
7. Provider adapter transforms request to provider wire format, sends to provider
8. Provider responds — adapter transforms response back to OpenAI/Anthropic format
9. Guardrail post-call hook runs (output filtering)
10. Cost tracker calculates token cost, updates per-key and per-user spend, checks budget
11. Cache writes response (if streaming: after full assembly)
12. Observability: Langfuse trace, Prometheus metrics, structured log
13. Response returned to agent with `x-litellm-response-cost` and rate limit headers

### 2. Fallback Flow

1. Steps 1-6 from happy path complete
2. Provider call fails (timeout, 500, 429)
3. Retry engine: exponential backoff with jitter, `Retry-After` header compliance
4. All retries exhausted — cooldown counter incremented for this deployment
5. Cooldown threshold hit — deployment marked as cooled down for `cooldown_time` seconds
6. Fallback chain activates: next model group runs through the full routing pipeline independently
7. If context window overflow — context window fallback routes to larger model
8. If all fallbacks exhausted — return error to agent with diagnostic headers

### 3. Key Lifecycle

1. Admin calls `POST /key/generate` with constraints (models, budget, rate limits, team)
2. Gateway generates `sk-` prefixed key, encrypts provider credential mappings
3. Key stored in Redis/Postgres with team_id, org_id, user_id, constraints
4. Agent uses key in `Authorization` header — gateway validates on every request
5. Admin monitors via `GET /key/info` (spend, remaining budget, rate limit usage)
6. Key rotation: `POST /key/{id}/regenerate` with grace period — old key valid during overlap
7. Key compromise: `POST /key/block` — instant revocation, all in-flight requests rejected
8. Key deletion: `DELETE /key/{id}` — permanent removal, spend logs retained

### 4. Configuration

1. Gateway reads `config.yaml` at startup: model deployments, routing strategy, provider credentials, cache config, rate limits
2. Environment variables substitute into config via `os.environ/VARIABLE_NAME` syntax
3. Model pricing loaded from `pricing.yaml` — input/output cost per million tokens per model
4. Config reload endpoint (v1.2) — update pricing and routing without restart
5. Custom routing plugin loaded via Python import path in config

## Proof of concept: agent calls GPT-4o through the gateway

An existing commandclaw agent currently calls OpenAI directly:

```python
# Before gateway — agent holds real credentials
llm = ChatOpenAI(
    base_url="https://api.openai.com/v1",
    api_key="sk-real-openai-key-NEVER-DO-THIS",
    model="gpt-4o"
)
agent = create_react_agent(llm, tools)
```

With the gateway, one config change:

```python
# After gateway — agent holds virtual key, gateway holds real credentials
llm = ChatOpenAI(
    base_url="http://commandclaw-gateway:4000/v1",
    api_key="sk-virtual-key-for-this-agent",
    model="gpt-4o"
)
agent = create_react_agent(llm, tools)
```

The agent code is identical except for two strings. The gateway handles:
- Routing `gpt-4o` to the correct OpenAI deployment (or Bedrock, or Azure — transparent to agent)
- Falling back to `gpt-4o-mini` if the primary deployment is down
- Blocking the request if the agent's $100/day budget is exhausted
- Caching the response if an identical prompt was seen in the last 24 hours
- Stripping PII from the prompt before it leaves the enterprise boundary
- Logging the full trace to Langfuse with cost, latency, tokens, model, team, and cache status
- Counting against the agent's 100 RPM limit and the team's 2000 RPM limit

The `config.yaml` that makes this work:

```yaml
model_list:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
      api_base: https://api.openai.com/v1
      rpm: 500
      tpm: 100000
    model_info:
      region: us-east-1

  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY_BACKUP
      api_base: https://api.openai.com/v1
      rpm: 500
      tpm: 100000
      order: 2  # failover — only used when primary is cooled down
    model_info:
      region: eu-west-1

  - model_name: claude-sonnet
    litellm_params:
      model: anthropic/claude-sonnet-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY
      rpm: 200
      tpm: 80000
    model_info:
      region: us-east-1

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 3
  timeout: 30
  allowed_fails: 3
  cooldown_time: 10
  fallbacks:
    - gpt-4o: [claude-sonnet]

cache_params:
  type: redis
  host: os.environ/REDIS_HOST
  port: 6379
  default_ttl: 86400

general_settings:
  master_key: os.environ/GATEWAY_MASTER_KEY

litellm_settings:
  success_callback: [langfuse, prometheus]
  failure_callback: [langfuse]
```

## Stack

| Component | Technology | Status |
|-----------|-----------|--------|
| API framework | FastAPI + Uvicorn | Available |
| HTTP client (provider calls) | httpx (async) | Available |
| Request/response models | Pydantic v2 | Available |
| Streaming (SSE) | FastAPI StreamingResponse / EventSourceResponse | Available |
| Cache + rate limiting + spend tracking | Redis 7+ | Available (commandclaw-observe stack) |
| Token counting | tiktoken | Available |
| Cost calculation | tokencost or custom YAML pricing table | Available / To build |
| RBAC | Cerbos (own instance) | Available |
| LLM tracing | Langfuse v4 SDK (@observe decorator) | Available (commandclaw-observe stack) |
| Metrics | prometheus_client | Available |
| Alerting | Slack webhook (httpx POST) | To build |
| Guardrails (PII) | Presidio | Available |
| Guardrails (generic) | Custom webhook interface | To build |
| Auth (v1) | API key validation (Redis lookup) | To build |
| Auth (v1.1) | JWT/OIDC (PyJWT + JWKS) | To build |
| Config | PyYAML + env var substitution | To build |
| Provider: OpenAI | httpx + baseline adapter | To build |
| Provider: Anthropic | httpx + custom adapter | To build |
| Provider: Vertex AI | httpx + Google auth | To build |
| Provider: Bedrock | httpx + SigV4 signing | To build |
| Provider: Ollama | Baseline adapter (OpenAI-compat) | To build |
| Semantic cache (v1.2) | RedisVL (vector similarity) | Available |
| Container | Docker (non-root, read-only fs in v1.1) | To build |
| Orchestration (v1.2) | Kubernetes + Helm | To build |
| Log archival (v1.2) | S3 / GCS | To build |
| Image signing (v1.1) | Cosign | Available |

## What makes this hard

**1. Streaming correctness across two wire formats.** OpenAI streams `chat.completion.chunk` objects with a single delta shape. Anthropic streams typed events (`message_start`, `content_block_start`, `content_block_delta`, `message_delta`, `message_stop`) with different JSON shapes per event type. The gateway must proxy both formats correctly — including backpressure handling, client disconnect detection (`await request.is_disconnected()`), buffering for cache assembly, and synthetic re-chunking on cache hit. This is the most bug-prone area of the entire codebase because streaming errors are silent and intermittent.

**2. Cache key correctness is a silent failure mode.** Include too few fields in the cache key (miss `temperature`) and the cache returns wrong responses. Include too many fields (include `stream`) and the cache never hits. Include non-deterministic fields (include a request timestamp) and the cache is useless. There is no test that catches a wrong cache key — you only discover it when a user gets someone else's response or when the hit rate is suspiciously zero. Mitigation: explicit allowlist of cache-key fields, unit tests that verify inclusion/exclusion of every field, and a `x-gateway-cache-key` debug header.

**3. Hierarchical budget enforcement under concurrent writes.** When 50 agents sharing a team budget all send requests simultaneously, the spend tracking must be atomic. Redis `INCRBYFLOAT` handles per-key atomicity, but the hierarchical check (key budget OK AND user budget OK AND team budget OK AND org budget OK) is a multi-key operation that can race. A request might pass the team budget check at $2,999, another request passes at $2,999.50, and both proceed — team spends $3,001 against a $3,000 limit. Mitigation: Redis Lua scripts for atomic multi-key budget checks, or accept eventual consistency with a small overshoot tolerance and periodic reconciliation.

**4. Provider adapter maintenance burden.** Each non-OpenAI provider has its own request format, response format, error format, streaming format, auth mechanism, and rate limit header conventions. Anthropic's Messages API is a fundamentally different wire protocol from OpenAI's chat completions. Bedrock requires SigV4 request signing. Vertex AI requires Google OAuth token refresh. When providers update their APIs (which happens quarterly), the adapters break silently — responses parse but contain wrong values. Mitigation: provider-specific integration tests that run against real APIs on a schedule, not just mocks.

**5. Semantic cache similarity threshold tuning.** Too high (0.99) and the semantic cache barely ever hits — might as well use exact match. Too low (0.85) and it returns cached responses for queries that are "close enough" but actually different, producing wrong answers. There is no universal right value — it depends on the use case, the embedding model, and the prompt structure. An enterprise chatbot answering password reset questions has different tolerance than a code generation agent. Mitigation: per-model/per-use-case threshold config, and a `x-gateway-semantic-similarity` response header so operators can tune empirically.

**6. Multi-tenant cache isolation vs. cost savings tradeoff.** Sharing the cache across teams maximizes hit rate and saves the most money. Isolating per team prevents cross-team data leakage. The enterprise customer wants both — maximum savings AND guaranteed isolation. The `cache_scope` config (global | team | key) pushes the decision to the operator, but the wrong default will either leak data or waste money at scale. Mitigation: default to `team`-scoped isolation (safe), let operators explicitly opt into `global` with a documented warning.

**7. Region-based routing interacts with every other routing constraint.** A request needs a model in `eu-west-1` (GDPR), but the only EU deployment is cooled down (reliability), and the fallback model is only deployed in `us-east-1` (region violation). The gateway must fail the request rather than violate the region constraint — but the error message must clearly explain why, not just return a generic 503. Every routing feature (fallbacks, canary splits, custom plugins) must respect region constraints as an inviolable filter, not an advisory preference.

## Why this works

The LLM gateway space is dominated by two failure modes. Managed services (OpenRouter, Cloudflare AI Gateway) are too opaque — enterprise can't self-host, can't audit, can't customize. Open-source platforms (LiteLLM, Portkey) are too sprawling — they absorb every adjacent concern until the gateway is indistinguishable from a full AI platform.

commandclaw-gateway avoids both by being opinionated about scope. It routes LLM traffic. It doesn't orchestrate agents (that's the runtime). It doesn't proxy tool calls (that's commandclaw-mcp). It doesn't host dashboards (that's Grafana). It doesn't store traces (that's Langfuse). It doesn't manage vector stores (that's the retrieval layer). Every time a feature doesn't fit the LLM routing mission, it gets built in the right service instead of bolted onto the gateway.

This works because CommandClaw already has the supporting infrastructure. Redis is running. Langfuse is running. Prometheus and Grafana are running. Cerbos is proven. The phantom token pattern is proven. The gateway is filling a specific gap in an existing ecosystem, not bootstrapping a platform from scratch.

The phased approach works because the v1 data model is designed for v1.2's needs. Adding teams doesn't require migrating the key table — the `team_id` column is already there. Adding JWT auth doesn't require rewriting the middleware — the pluggable auth slot is already there. Adding region routing doesn't require restructuring the router — the filter pipeline is already there. **The architecture is enterprise-ready on day one. The features arrive over four milestones.**

14 weeks to enterprise-ready. 25% of LiteLLM's effort for the features that matter. Zero dependency surface. One service, one job, one codebase a single engineer can hold in their head.

## Note

This document is intentionally abstract about implementation details — specific function signatures, database schemas, Redis key patterns, and error handling strategies depend on the engineering context at build time. The right way to use this document is to share it with an LLM coding agent alongside the detailed proposal (`litellm-feature-inventory-proposal.md`) and let the agent make implementation decisions within the architectural constraints defined here. The proposal has the exhaustive feature tables and build effort estimates. This document has the vision and the constraints.
