# LiteLLM Feature Inventory and Build-Scope Estimation for CommandClaw

**Author:** Background Research Agent (BEARY)
**Date:** 2026-04-16

---

## Abstract

This whitepaper decomposes every documented feature of LiteLLM — the dominant open-source LLM gateway — into a structured inventory organized by functional domain. For each domain, it estimates the engineering effort required to build an equivalent from scratch, classifies features into phased milestones, and identifies the small set of features that are genuinely out of scope. The inventory covers 37 documentation pages and catalogs approximately 400+ distinct features across 14 functional domains.

CommandClaw is an enterprise agent platform. Feature decisions in this proposal are evaluated against the planned enterprise-ready state, not the current development state. Features like multi-team isolation, SSO, guardrails, and audit trails are not "nice to have someday" — they are on the product roadmap and the gateway architecture must support them from day one, even where the implementation is phased.

The conclusion: v1 (~6 weeks) covers the core gateway with dual-format API, full virtual key lifecycle, and production observability. The enterprise-ready gateway (~10 weeks total) adds multi-tenant hierarchy, guardrails, SSO readiness, and comprehensive cost governance. Only ~15% of LiteLLM's features are genuinely out of scope (media endpoints, 93+ provider adapters, admin UI, protocol gateways handled by other commandclaw services).

---

## Evaluation Framework

Features are classified into four tiers:

| Tier | Meaning | Criteria |
|---|---|---|
| **v1 — Ship** | Required for a functional gateway | Cannot serve production traffic without it |
| **v1.x — Enterprise Foundation** | Required for enterprise readiness | Enterprise customers expect this; architecture must support it from v1 |
| **v2 — Optimize** | Improves performance, cost, or operations | Valuable but gateway works without it |
| **Skip** | Not gateway scope | Handled by another commandclaw service, or genuinely irrelevant |

The key shift from earlier drafts: features previously deferred as "small team doesn't need this" are now evaluated as "enterprise will need this — does the v1 architecture support adding it cleanly?" If the answer is no, the v1 architecture must be adjusted even if the feature itself is implemented later.

---

## 1. API Surface — Supported Endpoints

LiteLLM exposes an enormous endpoint surface that has grown well beyond LLM completion proxying [3]:

### Endpoints CommandClaw Needs

| Endpoint | Purpose | Build Effort |
|---|---|---|
| `POST /v1/chat/completions` | Core chat completion proxy — OpenAI format (streaming + non-streaming) | 2-3 days |
| `POST /v1/messages` | Anthropic Messages API format — native Anthropic support for commandclaw agents | 1 day |
| `POST /v1/messages/count_tokens` | Anthropic token counting endpoint | 2 hours |
| `GET /v1/models` | Model enumeration for clients (serves both formats) | 2 hours |
| `POST /v1/embeddings` | Embedding proxy for RAG pipelines | 1 day |
| `GET /health`, `/health/readiness`, `/health/liveliness` | K8s/Docker health probes | 2 hours |
| `GET /metrics` | Prometheus metrics endpoint | 1 day |

The `/v1/messages` endpoint requires its own Pydantic request/response models — Anthropic's wire format differs significantly from OpenAI's:
- `content` is an array of typed blocks (`text`, `image`, `tool_use`, `tool_result`) rather than a string
- `system` is a top-level field, not a message role
- `stop_reason` instead of `finish_reason`
- `usage` includes `cache_creation_input_tokens` and `cache_read_input_tokens`
- Streaming uses typed SSE events (`message_start`, `content_block_delta`, `message_delta`, `message_stop`) instead of OpenAI's single `chat.completion.chunk`

Both endpoints share the same provider adapters, routing, caching, auth, and observability infrastructure — only the request/response envelope differs. This makes the gateway a unified entry point: LangChain agents hit `/v1/chat/completions`, Anthropic SDK clients hit `/v1/messages`.

### Endpoints to Add Later (Enterprise-Relevant)

| Endpoint | Phase | Why Enterprise Needs It | Build Effort |
|---|---|---|---|
| `POST /v1/batches` | **v2** | Batch inference is 50% cheaper than real-time on most providers. Enterprise cost optimization at scale — run overnight batch jobs through the gateway for cost tracking, key validation, and audit trail. | 1 day |
| `POST /v1/responses` | **v2** | OpenAI is pushing the Responses API as the successor to chat completions. Enterprise customers on latest OpenAI SDK may expect it. | 1 day |

### Endpoints CommandClaw Does Not Need (Genuinely Out of Scope)

| Endpoint | Why Skip |
|---|---|
| `POST /v1/completions` | Legacy text completion; superseded by chat completions |
| `/audio/transcriptions`, `/audio/speech`, `/realtime` | Audio/media — different product, different traffic pattern |
| `/images/generations`, `/images/edits`, `/videos` | Image/video generation — different product |
| `/files`, `/vector_stores`, `/containers` | File/vector management — separate service concern |
| `/fine_tuning` | ML ops — managed via provider UIs or dedicated tooling |
| `/rag/ingest`, `/rag/query` | RAG pipeline — handled by LangGraph retrieval nodes |
| `/evals`, `/moderations` | Evaluation/moderation — Langfuse + guardrails section covers this |
| `/ocr`, `/search` | Specialized — not core gateway |
| `/rerank` | Could justify as gateway endpoint for RAG cost tracking, but lower priority than core |
| `/assistants` | Deprecated — shutting down Aug 2026 |
| `/a2a` | Agent orchestration — commandclaw's own routing |
| `/mcp` | Tool access control — commandclaw-mcp's job |
| `/converse`, `/invoke`, `/generateContent` | Provider-specific pass-through — defeats unified interface |
| `Pass-through Endpoints` (15+ providers) | Same — defeats unified interface |
| `/skills` | Anthropic-specific |

**Verdict:** LiteLLM exposes ~40 endpoint groups. CommandClaw needs 7 for v1, 2 more in v2 (batches + responses). The remaining 31 are handled by other commandclaw services or are genuinely different products.

---

## 2. Provider Support

LiteLLM supports 100+ providers and 2,500+ models [4][37]:

### Providers CommandClaw Needs (Current + Near-Term)

| Provider | Priority | Adapter Effort |
|---|---|---|
| OpenAI (GPT-4o, o3, etc.) | P0 — primary | 1 day (OpenAI-compatible = baseline) |
| Anthropic (Claude 4.x) | P0 — primary | 1-2 days (different message format) |
| Google Vertex AI / Gemini | P1 — secondary | 1-2 days |
| AWS Bedrock | P1 — secondary | 1-2 days (SigV4 auth) |
| Ollama (local) | P1 — dev/testing | 2 hours (OpenAI-compatible) |
| Groq | P2 — optional | 2 hours (OpenAI-compatible) |
| DeepSeek | P2 — optional | 2 hours (OpenAI-compatible) |

### Providers CommandClaw Can Skip

The remaining 93+ providers (AI21, Aleph Alpha, Cerebras, Clarifai, Cohere, DataRobot, Databricks, ElevenLabs, Fireworks, HuggingFace, Mistral, Replicate, RunwayML, Stability AI, Together AI, vLLM, xAI, etc.) are not needed at current scale. Any OpenAI-compatible provider works with zero adapter effort via the baseline adapter.

**Provider adapter architecture** (from LiteLLM's own pattern [4]):
- Each provider is a `BaseConfig` subclass with 4 methods: `validate_environment()`, `get_complete_url()`, `transform_request()`, `transform_response()`
- OpenAI-compatible providers need zero custom code — they use the baseline adapter
- Only providers with non-OpenAI wire formats need custom adapters (Anthropic, Bedrock, Vertex)

**Build effort for all P0+P1 providers: ~5 days.** Each additional OpenAI-compatible provider: 2 hours.

---

## 3. Routing and Load Balancing

LiteLLM's Router class implements 6 routing strategies [8][10]. For an enterprise platform, routing is where the gateway delivers its core value — and enterprise routing requirements are too varied for a fixed strategy menu.

### v1 — Built-in Strategies

| Strategy | What It Does | Build Effort |
|---|---|---|
| `simple-shuffle` | Random with RPM/TPM weights — recommended default | 1 day |
| `least-busy` | Fewest concurrent requests — good for spike absorption | 4 hours |

### v1 — Routing Infrastructure

| Feature | Why v1 | Build Effort |
|---|---|---|
| `order` — deployment priority tiers | Failover ordering is day-one | 1 hour |
| `weight` — weighted selection frequency | Built into shuffle | included |
| `max_parallel_requests` — per-deployment concurrency cap | Prevent overloading a single deployment | 1 hour |
| Model aliasing (`model_group_alias`) | Teams refer to models by friendly names | 1 hour |

The v1 router must be built as a **pipeline** (filter → strategy → select) so that enterprise features plug in cleanly:

```
Deployments
  → Filter: remove cooldown deployments
  → Filter: region constraint (v1.1)
  → Filter: context window pre-check (v1.1)
  → Strategy: select from survivors (shuffle, least-busy, or custom plugin)
  → Select: return deployment
```

**Build effort for v1 routing: ~2 days.**

### v1.1 — Enterprise Routing

| Feature | Why Enterprise Needs It | Build Effort |
|---|---|---|
| Custom routing plugin | Enterprise routing needs are too varied for fixed strategies (content-based, tier-based, time-based, budget-aware). Plugin ABC: `select_deployment(deployments, context) → deployment` | 1 day |
| Region-based routing constraint | GDPR/data residency compliance. Deployments tagged with `region`; keys/teams have `allowed_regions`. Router filters by region before applying strategy. Not optional for EU enterprise customers. | 4 hours |
| Traffic splitting (canary %) | Safe model migration. Route N% of traffic to new model, monitor error rate, auto-rollback on threshold breach. Enterprise needs safe rollout, not instant cutover. | 4 hours |
| Pre-call context window validation | Reject requests that will exceed model's context limit before burning tokens/latency | 2 hours |

**Custom routing plugin interface:**

```python
class RoutingStrategy(ABC):
    @abstractmethod
    async def select_deployment(
        self,
        deployments: list[Deployment],    # healthy, region-filtered candidates
        request: RoutingContext,           # model, messages, metadata, user, team, key
    ) -> Deployment:
        pass
```

Enterprise writes a Python class, registers via config:

```yaml
routing_strategy: custom
custom_routing_plugin: "mycompany.routing.TierBasedRouter"
```

**Build effort for v1.1 routing: ~2 days.**

### v2 — Optimization

| Strategy | What It Does | Build Effort |
|---|---|---|
| `latency-based-routing` | Route to fastest provider — uses rolling latency window | 1 day |
| `cost-based-routing` | Route to cheapest provider meeting quality bar | 4 hours |
| Traffic mirroring (A/B shadow) | Silent background request to evaluate new models without affecting response | 4 hours |

### Skip

| Feature | Why |
|---|---|
| `usage-based-routing` | Officially "not recommended" by LiteLLM — adds Redis call latency |
| Encrypted content affinity | Niche; no enterprise customer has asked for this |

---

## 4. Reliability — Fallbacks, Retries, Cooldowns

LiteLLM implements a sophisticated reliability stack [9]:

| Feature | What It Does | CommandClaw Need | Build Effort |
|---|---|---|---|
| Standard fallbacks | Fallback chain on any error | **Yes** | 4 hours |
| Context window fallbacks | Auto-route to larger model on overflow | **Yes** | 2 hours |
| Content policy fallbacks | Route to different provider on content block | **Defer** | 2 hours |
| Default fallbacks | Global fallback when all groups fail | **Yes** | 1 hour |
| `num_retries` | Configurable per-deployment | **Yes** | 2 hours |
| Exponential backoff with jitter | Rate limit retry handling | **Yes** | 2 hours |
| `Retry-After` header parsing | Provider-specified wait | **Yes** | 1 hour |
| Cooldown system (`allowed_fails`, `cooldown_time`) | Pause failing deployments | **Yes** | 4 hours |
| `RetryPolicy` class | Per-exception retry counts (see below) | **v2** | 2 hours |
| `AllowedFailsPolicy` | Per-exception cooldown thresholds (see below) | **v2** | 2 hours |
| Mock testing fallbacks | Simulate failures for testing | **v2** | 1 hour |
| Per-request `disable_fallbacks` | Client override | **v2** | 30 min |

**Build effort for core reliability (fallbacks + retries + cooldowns): ~2 days.**

### RetryPolicy and AllowedFailsPolicy — Per-Exception Error Handling (v2)

v1 uses flat values: all errors retry `num_retries` times, all errors trigger cooldown after `allowed_fails` failures. This is too blunt for production with multiple providers that have different reliability profiles.

**RetryPolicy** — controls how many times to retry based on the error type:

```python
RetryPolicy(
    BadRequestErrorRetries=0,        # 400 — don't retry, request is malformed
    AuthenticationErrorRetries=0,    # 401 — don't retry, key is wrong
    TimeoutErrorRetries=4,           # timeout — retry aggressively, likely transient
    RateLimitErrorRetries=3,         # 429 — retry with backoff
    ContentPolicyViolationRetries=0, # blocked content — retrying won't help
    InternalServerErrorRetries=2,    # 500 — retry, provider might recover
)
```

Without this, retrying a 401 wastes time and budget. Retrying a timeout 4 times makes sense because timeouts are transient. A flat `num_retries=3` for everything is wrong in both directions.

**AllowedFailsPolicy** — controls how many failures before a deployment goes into cooldown based on the error type:

```python
AllowedFailsPolicy(
    BadRequestErrorAllowedFails=1000,     # 400 — never cooldown, it's the caller's fault
    AuthenticationErrorAllowedFails=1,     # 401 — cooldown immediately, key is broken
    TimeoutErrorAllowedFails=5,            # timeout — tolerate a few before cooling down
    RateLimitErrorAllowedFails=10,         # 429 — expected during spikes, high tolerance
    ContentPolicyViolationAllowedFails=50, # blocked — content issue, not provider issue
    InternalServerErrorAllowedFails=3,     # 500 — provider is struggling, cooldown fast
)
```

Without this, a burst of 429s during a traffic spike could cooldown a healthy deployment and lose capacity. But a single 401 should trigger immediate cooldown because something is fundamentally wrong.

**How they interact:**

```
Request fails with 429
  → RetryPolicy: retry 3 times with exponential backoff
  → All 3 retries fail
  → AllowedFailsPolicy: increment 429 counter for this deployment
  → Counter < 10? → Accept next request normally
  → Counter ≥ 10? → Cooldown deployment for cooldown_time seconds
  → Fallback chain activates
```

**Why v2, not v1:** The v1 flat values (`num_retries=3`, `allowed_fails=3`) work for initial deployment. Per-exception policies become valuable when running multiple providers with different reliability profiles — Bedrock might timeout more than OpenAI, but OpenAI might rate-limit more aggressively. The implementation is small (2 hours each — replace a single integer with a dict lookup by exception type) because the retry and cooldown infrastructure already exists in v1.

---

## 5. Caching

LiteLLM supports 9 cache backends [11]. For an enterprise gateway, caching is not just a performance optimization — it is a cost governance mechanism. Every cache hit is a provider call that didn't happen: zero tokens, zero latency, zero cost. At enterprise scale with hundreds of agents, caching can reduce LLM spend by 20-40% on workloads with repeated prompts (system prompt templates, few-shot examples, common customer queries).

### v1 — Core Caching

#### Backends

| Backend | Why v1 | Build Effort |
|---|---|---|
| Redis exact cache | Primary — cluster-wide, persistent, fast | 1 day |
| In-memory cache | Dev/testing — no Redis dependency | 4 hours |

#### Per-Request Controls

| Control | What It Does | Build Effort |
|---|---|---|
| `ttl` | Cache duration override per request | included |
| `s-maxage` | Max acceptable cache age — return cached only if fresher than N seconds | included |
| `no-cache` | Force fresh response from provider (but may cache the new response) | included |
| `no-store` | Don't cache this response at all (sensitive data) | included |
| `namespace` | Custom cache partition — isolate by use case | included |
| `use-cache` | Opt-in when cache mode is `default_off` | included |

Build effort for controls: 2 hours (HTTP header/body parsing).

#### Cache Debugging

| Feature | Build Effort |
|---|---|
| `GET /cache/ping` — health check | 30 min |
| `DELETE /cache/delete` — manual invalidation | 30 min |
| `x-litellm-cache-key` response header | 30 min |
| `cache_hit: true` in response metadata | included |

#### Correctness Requirements (Must Get Right in v1)

**Cache key composition.** The key must include fields that affect output: `model`, `messages`, `temperature`, `top_p`, `max_tokens`, `tools`, `response_format`, `seed`, and any provider-specific params that change behavior. Must EXCLUDE fields that don't affect output: `stream` (same content, different delivery), `user` (metadata), `metadata` (logging). Getting this wrong in either direction breaks caching silently — either returning wrong responses or never hitting the cache. Build effort: 2 hours.

**Streaming cache assembly.** Streaming responses arrive as chunks over time. The gateway must:
1. Buffer the stream internally while forwarding chunks to the client in real-time
2. Assemble the complete response after `[DONE]`
3. Write to cache only after successful completion (not on error, not on client disconnect)
4. On cache hit for a streaming request: re-chunk the cached response into synthetic SSE events matching the expected chunk format

This interacts with the streaming implementation in Section 1. Build effort: 2 hours.

**Cache TTL per model.** Different models have different staleness profiles — a flat TTL is wrong:

| Model Type | Why Different TTL | Example TTL |
|---|---|---|
| GPT-4o, Claude Sonnet | Provider updates quarterly | 24 hours |
| Fine-tuned models | Never change unless retrained | 7 days |
| RAG-augmented responses | Underlying data changes | 5 minutes |
| Volatile models (beta, preview) | Behavior changes frequently | 1 hour |

Must be configurable per model in gateway config. Build effort: 1 hour.

**`supported_call_types` — which operations to cache.** Not all endpoints benefit from caching equally:
- Chat completions: high value (expensive, often repeated system prompts)
- Embeddings: lower value (cheaper, less likely to repeat exact input)
- Enterprise wants independent control: `cache_call_types: [completion, acompletion]`

Build effort: 1 hour.

**Cost accounting for cache hits.** A cache hit must be handled correctly across all systems:
- Log the request in Langfuse (for audit trail) with `cache_hit: true`
- Count against RPM rate limit (consumed gateway capacity)
- Do NOT count against TPM rate limit (no tokens processed by provider)
- Do NOT count against budget (no spend occurred)
- Return `x-litellm-response-cost: 0.0` header
- Increment `gateway_cache_hits_total` Prometheus counter

Without this, budget tracking and rate limiting give wrong answers. Build effort: 2 hours.

**Cache invalidation.**
- Manual: `DELETE /cache/delete?model=gpt-4o` — flush all cached responses for a model
- `DELETE /cache/flush` — flush entire cache
- Automatic on model version change: when gateway config reloads with a new `model_info.version`, invalidate cached responses for that model — stale responses are wrong responses

Build effort: 2 hours.

#### Cache Analytics (Prometheus)

You can't justify caching infrastructure without measuring it. Enterprise needs:

| Metric | Type | Labels | Why |
|---|---|---|---|
| `gateway_cache_hits_total` | Counter | model, team, cache_type | Is caching working? |
| `gateway_cache_misses_total` | Counter | model, team | What's the hit rate? |
| `gateway_cache_size_bytes` | Gauge | — | How much Redis memory? |
| `gateway_cache_evictions_total` | Counter | — | Are we running out of space? |
| `gateway_cache_latency_seconds` | Histogram | operation (get/set) | Is the cache itself slow? |

Build effort: 2 hours.

**v1 caching total: ~2.5 days.**

---

### v1.1 — Enterprise Caching

| Feature | Why Enterprise Needs It | Build Effort |
|---|---|---|
| Multi-tenant cache isolation (`cache_scope: global | team | key`) | Prevent cross-team cache leakage — one team's cached response must not serve another team if responses contain team-specific context. Configurable per team. | 2 hours |

Build effort: included in v1.1 total.

---

### v1.2 — Enterprise Caching at Scale

| Feature | Why Enterprise Needs It | Build Effort |
|---|---|---|
| Redis semantic cache | Enterprise customer-facing chatbots get paraphrased duplicates at scale. "How do I reset my password?" / "forgot password reset help" / "I need to change my password" — all same answer. Exact cache misses all of them. Semantic cache catches them. Cost savings at enterprise volume are significant. | 1 day |
| Semantic similarity threshold config (per model/use case) | Too high (0.99) → barely any hits. Too low (0.85) → returns wrong cached responses. Must be tunable. | included |
| Semantic cache embedding model config | Requires an embedding model for similarity search. Must be configurable: use gateway's own embedding endpoint or a dedicated lightweight model. | included |
| Redis Cluster / Sentinel | Enterprise won't accept a single Redis instance as SPOF for cache AND rate limiter AND spend tracking. HA is a reliability requirement, not optimization. | 2 hours |

**v1.2 caching total: ~1.5 days** (on top of v1.2 section total).

---

### v2 — Caching Optimization

| Feature | Why | Build Effort |
|---|---|---|
| Qdrant semantic cache | Alternative semantic backend for teams already running Qdrant — Redis semantic is sufficient for most | 2 days |
| Caching groups | Treat `gpt-4o` and `gpt-4o-2024-08-06` as same cache bucket — prevents cache fragmentation across model version aliases | 2 hours |

---

### Skip

| Feature | Why |
|---|---|
| S3 bucket cache | Redis is the enterprise caching standard. S3 is for log archival (Section 13), not caching. |
| GCS bucket cache | Same — object storage is wrong abstraction for LLM response caching. |
| Disk cache | No value over Redis in any deployment topology. |
| Cache warming / preloading | LLM responses are too context-dependent to pre-populate. |

---

## 6. Authentication and Virtual Keys

The virtual key system is the access control foundation of the gateway — without it, there is no budget enforcement, no per-agent isolation, no credential separation. CommandClaw already built the phantom token system in commandclaw-mcp, proving this architecture works. The LLM gateway applies the same pattern to a different resource type: virtual LLM keys map to provider credentials, with per-key budget and model constraints [15][22].

### Core Virtual Key System (Must Have)

| Feature | Why Essential | Build Effort |
|---|---|---|
| `POST /key/generate` with full params | Entry point for all access control | 4 hours |
| Key validation middleware (every request) | Auth gate — nothing passes without a valid key | 2 hours |
| Per-key model allowlist | Prevent agents accessing wrong models | 2 hours |
| Per-key `max_budget` + `budget_duration` | Hard spend ceiling per agent/user | 4 hours |
| Per-key `tpm_limit` + `rpm_limit` | Rate isolation between agents | 2 hours |
| Per-key `max_parallel_requests` | Prevent runaway agent loops | 2 hours |
| Key block/unblock (instant revocation) | Kill switch for compromised keys | 1 hour |
| Key info + list endpoints | Operational visibility | 2 hours |
| Key delete | Lifecycle management | 1 hour |
| Key rotation with grace period | Reuse pattern from commandclaw-mcp | 4 hours |
| Upperbound constraints on key generation | Prevent accidental over-provisioning | 2 hours |
| Default key generation params | Reduce config boilerplate | 1 hour |
| Spend tracking per key (Redis accumulation) | Budget enforcement depends on this | 2 hours |
| Key-to-user association | Attribution for cost tracking | 1 hour |
| `x-litellm-response-cost` header per response | Transparency for callers | 30 min |
| Credential encryption at rest (`SALT_KEY` equivalent) | Provider keys never stored in plaintext | 4 hours |

### Production Essentials (Also Must Have)

| Feature | Why Essential | Build Effort |
|---|---|---|
| Per-user budgets + spend tracking | Agents belong to users; need aggregate view | 4 hours |
| User management endpoints (`/user/new`, `/user/info`) | Operational necessity | 2 hours |
| Hierarchical budget enforcement (user > key) | If user is over budget, all their keys stop | 2 hours |
| Custom header for key passing | Flexibility for different client patterns | 30 min |
| Budget reset scheduler | Budgets must reset on duration boundaries | 2 hours |
| Temporary budget increase with expiry | Handle burst needs without permanent changes | 1 hour |

### v1.x — Enterprise Foundation (Architecture Must Support From Day One)

These are not optional future features — they are enterprise requirements. The v1 data model (key storage schema, budget tables, middleware chain) must be designed to support these without a rewrite.

| Feature | Why Enterprise Needs It | Build Effort | Phase |
|---|---|---|---|
| JWT/OIDC/SSO | Enterprise has Okta/Azure AD/Google — they will not use API keys for humans | 3-5 days | v1.1 |
| Team hierarchy + team budgets | Multi-team isolation is table stakes for enterprise — shared gateway with team-scoped keys, budgets, model access | 2 days | v1.1 |
| Organization hierarchy | Enterprise customers have business units; orgs own teams | 1 day | v1.2 |
| RBAC roles (`proxy_admin`, `team_admin`, `internal_user`) | Enterprise needs role-based access — not everyone is admin | 1 day (own Cerbos instance) | v1.1 |
| Role-based key generation restrictions | Non-admins generate keys within policy bounds | 4 hours | v1.1 |
| Audit trail (key creation, deletion, budget changes) | Enterprise compliance — who changed what, when | 1 day | v1.2 |
| Self-serve key management (API-driven, no UI needed) | Enterprise teams create their own keys within policy bounds | 4 hours | v1.1 |

**v1 architecture implications:**
- Key storage schema must have `team_id`, `org_id`, `user_role` columns from day one — even if teams/orgs are not enforced yet
- Budget enforcement must be a chain (org → team → user → key) — even if only key-level is active initially
- Middleware must have a pluggable auth step — API key auth in v1, JWT auth bolted on in v1.1 without changing the middleware chain
- All spend/rate-limit data must carry team/org dimensions in labels — even if not grouped by them yet

| Feature | Status |
|---|---|
| Custom key generation validation function | **Skip** — overengineered |
| K8s ServiceAccount auth | **Skip** — niche; add only if K8s-specific customer demands it |

The pattern maps directly to commandclaw-mcp: phantom tokens become virtual LLM keys, Redis session pooling becomes Redis budget/rate counters, Cerbos RBAC becomes model allowlists per key. Same architecture, different resource type. The gateway runs its own Cerbos instance — no shared dependency with commandclaw-mcp. Each service owns its own policy engine and policy definitions.

**Build effort: v1 virtual key system ~4 days. v1.1 enterprise auth (SSO + teams + RBAC) ~5 days. v1.2 (orgs + audit) ~2 days.**

---

## 7. Multi-Tenancy — Orgs, Teams, Users

LiteLLM's four-level hierarchy [17][18]. For an enterprise platform, multi-tenant isolation is not optional — it is a core requirement. The question is not "if" but "when each level ships."

| Level | Tier | Rationale | Build Effort |
|---|---|---|---|
| Virtual Keys | **v1 — Ship** | Foundation (Section 6) | included above |
| Users | **v1 — Ship** | Per-user budgets, spend, key association (Section 6) | included above |
| Teams | **v1.1 — Enterprise Foundation** | Multi-team isolation, team-scoped budgets/keys/models — enterprise table stakes | 2 days (Section 6) |
| Organizations | **v1.2 — Enterprise Foundation** | Business unit hierarchy above teams | 1 day (Section 6) |

| Feature | Tier | Rationale |
|---|---|---|
| Hierarchical budget enforcement (org → team → user → key) | **v1 architecture, v1.1 enforcement** | v1 schema must have the columns; v1.1 activates the chain |
| RBAC roles (`proxy_admin`, `team_admin`, `internal_user`) | **v1.1** | Own Cerbos instance — independent from commandclaw-mcp |
| Team member permissions (configurable read/write/admin) | **v1.1** | Enterprise teams need permission granularity |
| Self-serve key management (API-driven) | **v1.1** | Enterprise teams create their own keys within policy |
| SSO auto-provisioning (auto-create teams/users from IdP groups) | **v1.2** | Reduces onboarding friction at enterprise scale |
| Invitation system | **v2** | Convenience, not critical — API covers key distribution |
| Self-serve UI | **v2** | Only when team count warrants it — CLI/API sufficient until then |

**Build effort: covered in Section 6 phased plan.**

---

## 8. Cost Tracking and Budgets

LiteLLM's cost tracking surface [16][18]. The gateway owns all spend data — it is the only component that sees every token, every model, every request. Deferring cost features to Langfuse or Grafana is a mistake: Langfuse traces are observability artifacts, not financial records. The gateway should serve its own spend views.

### Core Features CommandClaw Needs

| Feature | Why Essential | Build Effort |
|---|---|---|
| Per-request cost calculation (`response_cost`) | Foundation — everything else depends on this | 4 hours |
| Token counting (tiktoken + fallback) | Required for cost calc and TPM rate limiting | 2 hours |
| Per-key spend accumulation | Budget enforcement depends on this | 2 hours (Redis counter) |
| Per-user spend tracking | Aggregate view across a user's keys | 1 hour |
| Budget enforcement (block on exceed) | Hard stop when budget is exhausted | 2 hours |
| `x-litellm-response-cost` response header | Transparency for callers | 30 min |
| Cost pricing table (per-model input/output rates) | Lookup table for cost calc | 2 hours (static YAML) |
| Custom spend tags (`metadata.tags`) | Answer "how much did summarization cost vs. chat?" — cost-per-use-case, not just cost-per-key | 2 hours |
| `GET /spend/logs` with filters | Financial records — "show me every dollar spent, by whom, on what model, when." Budget forensics when something goes wrong | 4 hours |
| Spend logs metadata (`metadata.spend_logs_metadata`) | Attach arbitrary context (`pipeline`, `customer`, `experiment`) to spend records for querying by any dimension | 1 hour |

### Nice-to-Have (Gateway-Relevant, Post-v1)

| Feature | Why It's Gateway | Effort |
|---|---|---|
| `GET /global/spend/report` with grouping (by team/model/tag/date) | Financial reporting view the gateway owns — Langfuse doesn't provide this, Grafana requires custom queries | 4 hours |
| `GET /user/daily/activity` | Daily breakdown of requests, tokens, cost per user — operational data the gateway owns | 2 hours |
| `POST /global/spend/reset` | Testing and fiscal period resets | 30 min |
| Pricing update mechanism (YAML reload endpoint or pricing API) | Update model pricing without redeploying | 2 hours |

### Correctly Skipped

| Feature | Why Skip |
|---|---|
| Customer/end-user budget management | SaaS billing pattern — commandclaw agents don't have "end customers" in that sense |

Model-specific budgets and agent iteration caps are covered in the nice-to-haves section (items #13 and #14).

**Build effort for core cost tracking: ~2.5 days.**

---

## 9. Rate Limiting

Enterprise rate limiting is much more than "N requests per minute per key." It requires multi-dimensional enforcement, token-awareness, hierarchical limits, upstream provider awareness, and the distinction between rate limits (throughput) and quotas (allocation) [12][18].

### Two Distinct Concepts

| Concept | What It Controls | Time Window | Example |
|---|---|---|---|
| **Rate limit** | Throughput — how fast | Per-minute, per-second | 100 RPM, 50k TPM |
| **Quota** | Allocation — how much total | Per-day, per-month, per-budget-cycle | 1M tokens/day, $500/month |

Both must exist independently. A team can have 100 RPM (rate limit) AND 10M tokens/month (quota). The rate limit prevents spikes; the quota prevents overspend. The budget system (Section 8) handles quotas. This section handles throughput rate limits. Both are enforced independently — a request must pass both.

### Multi-Dimensional Enforcement

Enterprise needs limits at every level of the hierarchy, enforced hierarchically — a request must pass ALL applicable limits:

```
Request arrives with key K, user U, team T, org O, model M
  → Check: key K RPM/TPM for model M    (per-key-per-model)
  → Check: key K RPM/TPM overall        (per-key)
  → Check: user U RPM/TPM               (per-user)
  → Check: team T RPM/TPM               (per-team)
  → Check: team T RPM/TPM for model M   (per-team-per-model)
  → Check: org O RPM/TPM                (per-org)
  → Check: model M RPM/TPM global       (per-model, org-wide)
  → All pass? → Route request
  → Any fail? → 429 with Retry-After header + which limit was hit
```

### Token-Aware Limits

A request sending 100k tokens is fundamentally different from one sending 100 tokens. Rate limits must support:
- **RPM** — requests per minute (throughput)
- **TPM** — tokens per minute (compute cost)
- **`token_rate_limit_type`** — configurable: count `input` tokens only, `output` only, or `total` (input + output)

### v1 — Core Rate Limiting

| Feature | Why v1 | Build Effort |
|---|---|---|
| Per-key RPM | Baseline isolation between agents/services | 2 hours |
| Per-key TPM | A single 100k-token request can exhaust provider allocation | 2 hours |
| Redis sliding window algorithm | Cluster-wide consistency across multiple gateway workers | included |
| Rate limit response headers (`x-ratelimit-remaining-requests`, `x-ratelimit-remaining-tokens`) | Callers can self-throttle before hitting limits | 1 hour |
| 429 response with `Retry-After` header | Standard HTTP rate limit contract | 30 min |

The v1 implementation must use **Redis-backed sliding windows** from day one — in-memory rate limiting breaks in multi-worker deployments. The Redis key structure must include dimension placeholders (`key:{key_id}:model:{model}:rpm`) so that multi-dimensional enforcement (v1.1) is a config change, not a rewrite.

**Build effort for v1 rate limiting: ~0.5 days.**

### v1.1 — Enterprise Rate Limiting

| Feature | Why Enterprise Needs It | Build Effort |
|---|---|---|
| Per-key-per-model limits | "This key gets 50 RPM on GPT-4o but 200 RPM on Haiku" — control access to expensive models granularly | 2 hours |
| Per-model RPM/TPM (org-wide) | Protect expensive models from overconsumption regardless of who's calling | 2 hours |
| Per-team rate limits | Team-level capacity allocation — comes naturally with team hierarchy | 1 hour |
| Per-user rate limits | Aggregate across a user's keys | 1 hour |
| `token_rate_limit_type` (input/output/total) | Enterprise wants to limit by input tokens (cost driver) vs output tokens (different cost) | 1 hour |
| Daily/monthly token quotas (RPD/TPD) | "This team gets 1M tokens/day" — distinct from per-minute rate limits | 2 hours |
| Upstream provider rate limit awareness | Read provider `x-ratelimit-remaining-*` headers; filter out deployments at their provider limit before routing | 4 hours |
| Dynamic limit adjustment API (`PATCH /key/{id}`, `PATCH /team/{id}`) | Enterprise needs to change limits without redeploying the gateway | 2 hours |
| Rate limit hit logging (who, when, which limit) | Audit trail — enterprise needs to know which teams are hitting limits | 1 hour |

**Build effort for v1.1 rate limiting: ~2 days.**

### v2 — Optimization

| Feature | Why | Build Effort |
|---|---|---|
| Token bucket algorithm (burst allowance) | Sliding windows reject bursts. Agent loops fire 20 calls rapidly then wait for tool results — token bucket allows a burst of 50 requests then refills at 10/sec | 4 hours |
| Per-org rate limits | Comes with org hierarchy — org ceiling above team limits | 1 hour |
| Priority tiers | When capacity is constrained, high-priority keys/teams get served first | 4 hours |
| Fair queuing | Among same-priority callers, distribute capacity proportionally rather than first-come-first-served | 4 hours |

**Build effort for v2 rate limiting: ~1.5 days.**

### Skip

| Feature | Why |
|---|---|
| Budget tiers (LiteLLM enterprise) | Replaced by hierarchical budget enforcement (Section 6) + per-key/team/model limits — same outcome, cleaner architecture |
| Preemptive request bumping | Too aggressive — priority tiers + fair queuing covers the need |

---

## 10. Observability

LiteLLM's observability surface is massive [24][25][26][27][29][30]:

### Prometheus Metrics (40+ metrics)

CommandClaw needs a subset:

| Metric | Need |
|---|---|
| `litellm_spend_metric` (counter by key/team/model) | **Yes** |
| `litellm_input_tokens_metric`, `litellm_output_tokens_metric` | **Yes** |
| `litellm_request_total_latency_metric` (histogram) | **Yes** |
| `litellm_llm_api_latency_metric` (provider-only latency) | **Yes** |
| `litellm_llm_api_time_to_first_token_metric` | **Yes** |
| `litellm_proxy_total_requests_metric` | **Yes** |
| `litellm_proxy_failed_requests_metric` | **Yes** |
| `litellm_deployment_state` (health gauge) | **Yes** |
| Budget remaining gauges | **Defer** |
| Rate limit remaining gauges | **Defer** |
| Redis/system health metrics | **Defer** |
| Callback delivery monitoring | **Defer** |

Build effort for ~10 core Prometheus metrics: **1 day** (using `prometheus_client`).

### Logging Destinations (25+)

CommandClaw needs:
- **Langfuse** — already in use, `@observe` decorator or callback
- **Prometheus** — already in commandclaw-observe
- **Structured JSON logs** — standard Python logging

The remaining 22+ destinations (S3, SQS, Azure Blob, DynamoDB, GCS, PubSub, Datadog, Sentry, Arize, Lunary, etc.) are not needed.

### Alerting (4 channels, 15+ alert types)

CommandClaw needs:
- Slack webhook for budget alerts and deployment outages: **1 day**
- Everything else (Discord, Teams, digest mode, regional outage detection): **Defer**

### Custom Callbacks (6 hook points)

CommandClaw needs:
- `async_log_success_event` and `async_log_failure_event`: **Yes**, for Langfuse
- The remaining hooks (`log_pre_api_call`, `log_post_api_call`, etc.): **Defer**

Build effort for callback system: **4 hours** (simple event emitter pattern).

**Total observability build effort: ~3 days.**

---

## 11. Guardrails

LiteLLM supports 8 guardrail providers with pre/post/during-call modes [28]. For an enterprise platform, guardrails are not optional — enterprise customers will ask "how do you prevent PII from leaving our environment?" and "how do you detect prompt injection?" before signing.

### Guardrail Architecture (v1 — Must Build the Framework)

The gateway must have a **pluggable guardrail middleware chain** from v1, even if no specific guardrail providers are wired in yet. This means:
- Pre-call hook point (before provider call) — for input validation, PII detection, prompt injection
- Post-call hook point (after provider response) — for output filtering, PII in responses
- Per-key guardrail assignment — different keys can have different guardrail policies
- `x-litellm-applied-guardrails` response header — transparency about what ran

Build effort for the framework: **4 hours** (it's just two hook points in the middleware chain + config).

### Guardrail Providers (Phased)

| Provider | Tier | Why | Build Effort |
|---|---|---|---|
| Generic guardrail API (`generic_guardrail_api`) | **v1.1** | Extension point — lets enterprise plug in their own guardrail service without gateway changes | 4 hours |
| Presidio (PII detection/redaction) | **v1.1** | Most common enterprise compliance requirement — runs locally, no external dependency | 1 day |
| Prompt injection detection (Lakera or equivalent) | **v1.2** | Second most common enterprise security concern | 4 hours (via generic API) |
| Bedrock Guardrails | **v2** | Useful for AWS-centric enterprises | 4 hours |
| Aporia, guardrails_ai, Azure Text Moderations, AIM | **Skip** | Niche providers — generic API covers them |  |

### Enterprise Guardrail Features

| Feature | Tier | Why |
|---|---|---|
| Per-key guardrail assignment | **v1.1** | Different agents need different policies |
| Per-team guardrail defaults | **v1.2** | Team-level compliance policies |
| `mask_input` / `mask_output` per request | **v1.1** | Selective PII redaction in logs |
| Guardrail execution tracing (pass/fail in Langfuse) | **v1.1** | Audit trail for compliance |

**Build effort: v1 framework ~4 hours. v1.1 (generic API + Presidio + per-key assignment) ~2 days. v1.2 (injection detection + team defaults) ~1 day.**

---

## 12. Health Checks

LiteLLM's health system [31]:

| Feature | CommandClaw Need | Build Effort |
|---|---|---|
| `GET /health/liveliness` | **Yes** | 30 min |
| `GET /health/readiness` (DB check) | **Yes** | 30 min |
| `GET /health` (model health via API calls) | **Defer** | 1 day |
| Background health checks | **Defer** | 4 hours |
| Cross-pod health sync via Redis | **Skip** | — |

**Build effort for basic health: ~1 hour.**

---

## 13. Deployment and Infrastructure

LiteLLM's deployment surface [6][7]. Enterprise customers deploy on Kubernetes, require security-hardened containers, and may need multi-region architecture.

### v1 — Core Deployment

| Feature | Build Effort |
|---|---|
| Docker image (Dockerfile + .dockerignore) | 2 hours |
| Docker Compose service entry (integrate with existing stack) | 2 hours |

**Build effort: ~0.5 days.**

### v1.1 — Enterprise Security Hardening

| Feature | Why Enterprise Needs It | Build Effort |
|---|---|---|
| Non-root image (UID 101, all capabilities dropped) | Enterprise security baseline — containers must not run as root | 2 hours |
| Read-only filesystem (EmptyDir volumes for writable paths) | Prevents runtime tampering — enterprise security requirement | 2 hours |
| Cosign image signing | Supply chain security — enterprise verifies image provenance | 2 hours |

**Build effort: ~1 day.**

### v1.2 — Enterprise Deployment

| Feature | Why Enterprise Needs It | Build Effort |
|---|---|---|
| Kubernetes manifests (ConfigMap, Secrets, HPA, liveness/readiness probes) | Enterprise deploys on K8s, not Docker Compose | 1 day |
| Helm chart | K8s deployment convenience — `helm install` with `values.yaml` overrides | 1 day |
| S3/GCS log archival | Compliance — enterprise audit trail for all LLM requests stored in object storage | 4 hours |

**Build effort: ~2.5 days.**

### v2 — Scale

| Feature | Why | Build Effort |
|---|---|---|
| Control plane / data plane split | Multi-region enterprise — centralized admin, regional workers. Admin instance runs UI/key management, workers run LLM routing only with `DISABLE_ADMIN_ENDPOINTS=true` | 1 day |
| Cross-pod health sync via Redis | Multi-instance health state sharing — one pod detects outage, all pods stop routing to it | 4 hours |

**Build effort: ~1.5 days.**

### Skip

| Feature | Why |
|---|---|
| Terraform provider | Overkill — K8s + Helm covers IaC needs |
| Cloud-specific (Cloud Run, Railway, Render) | Vendor lock-in — Dockerfile is universal |
| AWS CloudFormation stack | Same — K8s is the enterprise standard |
| HTTP/2 via Hypercorn | Marginal benefit — standard HTTP/1.1 + streaming is sufficient |

---

## 14. Admin UI

LiteLLM ships a web dashboard for key management, model management, spend tracking, and AI Hub [32].

**CommandClaw need: Skip entirely.** Admin manages keys via CLI/API. Grafana handles dashboards. The UI is a significant engineering effort (~2-4 weeks) with no value for a small team.

---

## 15. Protocol Support (A2A, MCP)

LiteLLM supports A2A agent protocol and MCP gateway [35][36]:

- **A2A**: Agent-to-agent protocol with JSON-RPC 2.0, iteration budgets, streaming. **Skip** — commandclaw has its own agent routing.
- **MCP**: Tool listing, calling, prompts, resources, 6 auth methods, 3 transports. **Skip** — commandclaw-mcp already handles this.

---

## Scope Estimation Summary

### What CommandClaw Needs (Build Scope)

| Domain | Features Needed | Est. Build Days |
|---|---|---|
| **API Surface** | `/chat/completions`, `/messages`, `/messages/count_tokens`, `/models`, `/embeddings`, health, metrics | 4.5 |
| **Provider Adapters** | OpenAI, Anthropic, Vertex, Bedrock, Ollama | 5 |
| **Routing** | Shuffle, least-busy, ordered priority, weights | 2 |
| **Reliability** | Fallbacks, retries, exponential backoff, cooldowns | 2 |
| **Caching** | Redis exact + in-memory, streaming assembly, per-request controls, per-model TTL, cache invalidation, cost accounting for hits, cache analytics (Prometheus), `supported_call_types` | 2.5 |
| **Auth & Virtual Keys** | Full key lifecycle, rotation, encryption, per-key constraints, user association, hierarchical budgets | 4 |
| **Cost Tracking** | Per-request cost calc, token counting, per-key/user spend, budget enforcement, spend tags, spend logs with metadata | 2.5 |
| **Rate Limiting** | Per-key RPM/TPM via Redis sliding window, response headers, 429 + Retry-After | 0.5 |
| **Observability** | ~10 Prometheus metrics, Langfuse callbacks, Slack alerts | 3 |
| **Health Checks** | Liveness, readiness | 0.5 |
| **Deployment** | Dockerfile, docker-compose entry | 0.5 |
| **Config** | YAML config loader, env var substitution | 1 |
| | | |
| **Total** | | **~28 days (~6 weeks, 1 engineer)** |

### v1.1 — Enterprise Foundation (~2 weeks)

Features enterprise customers expect. The v1 architecture is designed to support these without refactoring.

#### Auth & Multi-Tenancy

| Feature | Why Enterprise Needs It | Effort |
|---|---|---|
| JWT/OIDC/SSO | Enterprise has Okta/Azure AD — they will not use raw API keys for humans | 3-5 days |
| Team hierarchy + team budgets | Multi-team isolation is table stakes — shared gateway with team-scoped keys, budgets, model access | 2 days |
| RBAC roles (`proxy_admin`, `team_admin`, `internal_user`) | Not everyone is admin | 1 day (own Cerbos instance) |
| Role-based key generation restrictions | Non-admins generate keys within policy bounds | 4 hours |
| Self-serve key management (API-driven) | Enterprise teams create their own keys within policy | 4 hours |

#### Guardrails

| Feature | Why Enterprise Needs It | Effort |
|---|---|---|
| Generic guardrail API | Extension point — enterprise plugs in their own guardrail service | 4 hours |
| Presidio PII detection/redaction | Most common compliance requirement — runs locally | 1 day |
| Per-key guardrail assignment | Different agents need different policies | 4 hours |
| `mask_input` / `mask_output` per request | Selective PII redaction in logs | 2 hours |
| Guardrail execution tracing (pass/fail in Langfuse) | Audit trail for compliance | 2 hours |

#### Routing (Enterprise)

| Feature | Why Enterprise Needs It | Effort |
|---|---|---|
| Custom routing plugin (`RoutingStrategy` ABC) | Enterprise routing needs are too varied for fixed strategies — content-based, tier-based, time-based, budget-aware | 1 day |
| Region-based routing constraint | GDPR/data residency — EU data stays on EU endpoints. Not optional for EU enterprise customers | 4 hours |
| Traffic splitting (canary %) | Safe model migration — route N% to new model, monitor, auto-rollback | 4 hours |

#### Caching (Enterprise)

| Feature | Why Enterprise Needs It | Effort |
|---|---|---|
| Multi-tenant cache isolation (`cache_scope: global | team | key`) | Prevent cross-team cache leakage — one team's cached response must not serve another team if responses contain team-specific context | 2 hours |

#### Security Hardening

| Feature | Why Enterprise Needs It | Effort |
|---|---|---|
| Non-root container image | Enterprise security baseline — containers must not run as root | 2 hours |
| Read-only filesystem | Prevents runtime tampering | 2 hours |
| Cosign image signing | Supply chain security — enterprise verifies image provenance | 2 hours |

#### Rate Limiting (Enterprise)

| Feature | Why Enterprise Needs It | Effort |
|---|---|---|
| Per-key-per-model limits | "This key gets 50 RPM on GPT-4o but 200 RPM on Haiku" — granular expensive model control | 2 hours |
| Per-model RPM/TPM (org-wide) | Protect expensive models from overconsumption regardless of caller | 2 hours |
| Per-team rate limits | Team-level capacity allocation — comes with team hierarchy | 1 hour |
| Per-user rate limits | Aggregate across a user's keys | 1 hour |
| `token_rate_limit_type` (input/output/total) | Enterprise controls whether to count input tokens, output tokens, or both | 1 hour |
| Daily/monthly token quotas (RPD/TPD) | "This team gets 1M tokens/day" — distinct from per-minute throughput limits | 2 hours |
| Upstream provider rate limit awareness | Read provider `x-ratelimit-remaining-*` headers; filter depleted deployments before routing | 4 hours |
| Dynamic limit adjustment API (`PATCH /key/{id}`, `PATCH /team/{id}`) | Change limits without redeploying the gateway | 2 hours |
| Rate limit hit logging (who, when, which limit) | Audit trail — enterprise needs to know which teams are hitting limits | 1 hour |

#### Safety & Cost Governance

| Feature | Why Enterprise Needs It | Effort |
|---|---|---|
| Pre-call context window validation | Reject requests that will fail before burning tokens | 2 hours |
| Agent iteration caps / session budgets | Runaway agent loops are the #1 LLM cost risk | 4 hours |
| Model-specific budgets | Cap spend per model — prevents one model eating all budget | 4 hours |
| Deployment health metrics + outage alerts | Gateway detects provider outages first | 1 day |

**v1.1 total: ~15.5 days (~3 weeks)**

---

### v1.2 — Enterprise Maturity (~1 week)

| Feature | Why | Effort |
|---|---|---|
| Organization hierarchy (above teams) | Enterprise has business units; orgs own teams | 1 day |
| Audit trail (key creation, deletion, budget changes) | Enterprise compliance — who changed what, when | 1 day |
| Prompt injection detection (via generic guardrail API) | Second most common enterprise security concern | 4 hours |
| Per-team guardrail defaults | Team-level compliance policies | 4 hours |
| SSO auto-provisioning (auto-create teams/users from IdP groups) | Reduces onboarding friction at scale | 4 hours |
| Per-org rate limits | Org ceiling above team limits — comes with org hierarchy | 1 hour |
| `GET /global/spend/report` with grouping (by team/model/tag/date) | Financial reporting view the gateway owns | 4 hours |
| Spend report alerts (daily/weekly Slack) | Automated cost visibility | 4 hours |
| Redis semantic cache | Paraphrased duplicates at enterprise chatbot scale — exact cache misses them, semantic catches them | 1 day |
| Redis Cluster / Sentinel | Enterprise HA — single Redis is SPOF for cache + rate limiter + spend tracking | 2 hours |
| Kubernetes manifests (ConfigMap, Secrets, HPA, probes) | Enterprise deploys on K8s | 1 day |
| Helm chart | K8s deployment convenience | 1 day |
| S3/GCS log archival | Compliance audit trail for all LLM requests | 4 hours |

**v1.2 total: ~10 days (~2 weeks)**

---

### v2 — Optimization (~1.5 weeks)

Features that improve performance, cost efficiency, and operational maturity. Gateway works without them, but they make it significantly better.

#### Routing & Reliability

| Feature | Why It's Gateway | Effort |
|---|---|---|
| Latency-based routing | Route to fastest provider — rolling latency window | 1 day |
| Cost-based routing | Route to cheapest provider meeting quality bar | 4 hours |
| Content policy fallbacks | Auto-reroute when provider blocks content | 2 hours |
| Traffic mirroring (A/B shadow) | Silent background request to evaluate new models | 4 hours |
| `RetryPolicy` per-exception | Different retry budgets for 429 vs 500 vs timeout | 2 hours |
| `AllowedFailsPolicy` per-exception | Different cooldown thresholds per error type | 2 hours |

#### Caching

| Feature | Why It's Gateway | Effort |
|---|---|---|
| Semantic caching (Redis or Qdrant) | Catch paraphrased duplicates that exact cache misses | 1-2 days |
| Caching groups | Treat model versions as same cache bucket | 2 hours |
| Redis Cluster / Sentinel | HA for cache and rate limit layer | 2 hours |

#### Observability & Alerting

| Feature | Why It's Gateway | Effort |
|---|---|---|
| Budget remaining gauges (Prometheus) | Alert before keys hit zero, not after | 2 hours |
| Rate limit remaining gauges | See which keys are approaching limits | 2 hours |
| Background health checks | Proactive health probing, not just reactive | 4 hours |
| Slow response alerts (`alerting_threshold`) | Detect provider degradation before users notice | 2 hours |
| Callback delivery monitoring | Know when logging itself is failing | 2 hours |
| `log_raw_request` (raw CURL to providers) | Debug provider-specific wire format issues | 2 hours |
| `GET /user/daily/activity` | Daily user consumption breakdown | 2 hours |

#### Rate Limiting (Advanced)

| Feature | Why It's Gateway | Effort |
|---|---|---|
| Token bucket algorithm (burst allowance) | Sliding windows reject bursts. Agent loops fire 20 calls rapidly then wait — token bucket allows burst then refills steadily | 4 hours |
| Priority tiers | When capacity is constrained, high-priority keys/teams get served first | 4 hours |
| Fair queuing | Among same-priority callers, distribute capacity proportionally rather than first-come-first-served | 4 hours |

#### Cost & Config

| Feature | Why It's Gateway | Effort |
|---|---|---|
| Pricing update mechanism (reload without redeploy) | Providers change pricing quarterly | 2 hours |
| `POST /global/spend/reset` | Testing and fiscal period resets | 30 min |
| Mock testing fallbacks | CI/CD for gateway config changes | 1 hour |
| Per-request `disable_fallbacks` | "I want exactly this model or nothing" | 30 min |
| Fallback report (weekly summary) | Provider reliability trends | 2 hours |

#### Endpoints

| Feature | Why | Effort |
|---|---|---|
| `POST /v1/batches` | Batch inference at 50% cost savings — enterprise cost optimization at scale | 1 day |
| `POST /v1/responses` | OpenAI Responses API — successor to chat completions, enterprise on latest SDK may expect it | 1 day |

#### Infrastructure

| Feature | Why | Effort |
|---|---|---|
| Control plane / data plane split | Multi-region enterprise — centralized admin + regional workers | 1 day |
| Cross-pod health sync via Redis | Multi-instance health sharing — one pod detects outage, all pods stop routing | 4 hours |

**v2 total: ~11.5 days (~2.5 weeks)**

---

### What to Actually Skip (Not Gateway Scope)

These remain out of scope regardless of enterprise scale — they are either handled by another commandclaw service or are genuinely a different product.

| Feature | Why It's Not Gateway |
|---|---|
| A2A agent protocol | Agent orchestration — commandclaw's own routing |
| MCP tool gateway | Tool access control — commandclaw-mcp's job |
| RAG/vector stores/files/containers | Storage and retrieval — separate service |
| Fine-tuning | ML ops — provider UIs or dedicated tooling |
| Audio/images/video endpoints | Media generation — different product entirely |
| Admin UI | 3-4 weeks of frontend; CLI + API + Grafana covers enterprise needs |
| 22+ logging destinations (beyond Langfuse, Prometheus, S3) | Integration sprawl — three destinations covers enterprise needs |
| Pass-through endpoints (15+ providers) | Defeats the unified interface philosophy |
| 93+ provider adapters | 5-7 providers + OpenAI-compat baseline covers enterprise needs |
| Cloud-specific deployment (Cloud Run, Railway, Render, CloudFormation) | Vendor lock-in — Dockerfile + Helm is the enterprise standard |
| K8s ServiceAccount auth | Niche — add only if K8s-specific customer demands it |
| Custom key generation validation function | Overengineered — role-based restrictions cover the need |
| Terraform provider | K8s + Helm covers IaC needs |
| HTTP/2 via Hypercorn | Marginal benefit over HTTP/1.1 + SSE streaming |
| Disk cache, S3 cache, GCS cache | Redis is the enterprise caching standard |
| Preemptive request bumping | Too aggressive — priority tiers + fair queuing covers the need |
| `usage-based-routing` | Officially not recommended by LiteLLM — adds Redis latency |
| Encrypted content affinity | Niche — no enterprise customer has asked for this |

---

## Revised Build Recommendation

The previous whitepaper recommended **Option C: hybrid with LiteLLM SDK as a library**. Given the vulnerability concern and this feature analysis, the recommendation shifts to:

### Option D: Build a Standalone Custom Gateway (No LiteLLM Dependency)

**Rationale:**
1. CommandClaw needs ~15% of LiteLLM's features — the vast majority is irrelevant scope.
2. LiteLLM's Python package imports 100+ provider modules, adding cold-start overhead, dependency surface, and vulnerability exposure even when using only the SDK.
3. The core patterns (provider adapters, routing, fallbacks, caching, virtual keys, cost tracking) are well-documented and straightforward to implement.
4. CommandClaw already has FastAPI, Redis, Langfuse, Prometheus, and virtual key patterns in commandclaw-mcp — the building blocks exist.
5. A custom gateway with 5-7 provider adapters is ~2,000-3,000 lines of Python — maintainable by one engineer.

**Architecture:**

```
commandclaw-gateway/
  main.py              # FastAPI app, route registration
  config.py            # YAML config loader, env var substitution
  providers/
    base.py            # BaseLLMProvider ABC (validate_env, get_url, transform_req/resp)
    openai.py          # OpenAI adapter (baseline for all OpenAI-compat providers)
    anthropic.py       # Anthropic adapter (Messages API wire format)
    vertex.py          # Vertex AI adapter
    bedrock.py         # AWS Bedrock adapter (SigV4 auth)
  routes/
    chat.py            # POST /v1/chat/completions (OpenAI format, streaming + non-streaming)
    messages.py        # POST /v1/messages, /v1/messages/count_tokens (Anthropic format)
    embeddings.py      # POST /v1/embeddings
    models.py          # GET /v1/models
    health.py          # GET /health, /health/readiness, /health/liveliness
    keys.py            # POST /key/generate, /key/info, /key/block, /key/delete, etc.
    users.py           # POST /user/new, GET /user/info
  auth/
    virtual_keys.py    # Key generation, validation, encryption, rotation
    budgets.py         # Budget enforcement, spend tracking, reset scheduler
    middleware.py      # Request auth middleware (every request)
  routing/
    router.py          # Deployment selection (shuffle, least-busy, ordered priority)
    fallbacks.py       # Fallback chains, context window fallbacks
    cooldowns.py       # Deployment cooldown tracking
    retries.py         # Exponential backoff with jitter, Retry-After parsing
  middleware/
    rate_limiter.py    # Redis-backed RPM/TPM per key
    cost_tracker.py    # Token counting (tiktoken), spend accumulation, pricing table
    cache.py           # Redis exact cache + in-memory, per-request controls
  observability/
    metrics.py         # Prometheus counters/histograms (~10 core metrics)
    callbacks.py       # Langfuse tracing + Slack alerting
  schemas/
    openai.py          # Pydantic models for OpenAI request/response/chunks
    anthropic.py       # Pydantic models for Anthropic request/response/events
    common.py          # Shared types (usage, error responses)
```

**Estimated effort: ~6 weeks for one engineer.** This produces a gateway that:
- Serves both OpenAI and Anthropic API formats from a single service
- Has zero dependency on LiteLLM (no vulnerability exposure)
- Implements full virtual key lifecycle with budget enforcement, rotation, and encryption
- Reuses commandclaw-mcp's Redis, auth patterns, and observability stack
- Integrates natively with commandclaw-observe (Prometheus + Grafana)
- Provides a clean migration path to add features (teams, JWT, guardrails) as needed

**What you lose vs. LiteLLM:** Automatic pricing updates, community-maintained provider adapters, admin UI, 100+ pre-built provider integrations. None of these are needed at CommandClaw's current scale.

**Phased build plan:**
- **v1 (~6 weeks):** Core gateway — dual-format API (OpenAI + Anthropic), 5 providers, routing (shuffle + least-busy), reliability, full virtual key system, cost tracking with spend tags/logs, per-key rate limiting (Redis sliding window), caching (Redis exact + in-memory + streaming assembly + per-model TTL + cache invalidation + cost accounting for hits + analytics + supported_call_types), observability. Architecture supports all enterprise features without refactoring.
- **v1.1 (~3 weeks):** Enterprise foundation — JWT/OIDC/SSO, team hierarchy + budgets, RBAC, guardrails (Presidio PII + generic API), custom routing plugin, region-based routing, traffic splitting (canary), multi-dimensional rate limiting (per-key-per-model, per-team, per-user, per-model org-wide, daily/monthly quotas, upstream provider awareness, dynamic adjustment API), agent iteration caps, multi-tenant cache isolation, security hardening (non-root, read-only filesystem, Cosign), deployment health + outage alerts.
- **v1.2 (~2 weeks):** Enterprise maturity — organization hierarchy + per-org rate limits, audit trail, prompt injection detection, team guardrail defaults, SSO auto-provisioning, spend reporting API + alerts, Redis semantic cache + Redis Cluster/Sentinel HA, Kubernetes manifests + Helm chart, S3/GCS log archival for compliance.
- **v2 (~2.5 weeks):** Optimization — latency/cost-based routing, traffic mirroring, content policy fallbacks, token bucket algorithm + priority tiers + fair queuing, per-exception retry/cooldown policies, Qdrant semantic cache + caching groups, batch inference endpoint, OpenAI Responses API, control plane/data plane split, cross-pod health sync, background health, full observability gauges.
- **Enterprise-ready gateway: ~14 weeks total (~3.5 months, 1 engineer)**

---

## Conclusion

LiteLLM is a sprawling platform with ~400+ features across 14 functional domains. Its documentation reveals a product that has grown from a simple LLM proxy into a full AI platform with agent gateways (A2A), tool servers (MCP), file management, RAG pipelines, fine-tuning, evaluations, and more. For CommandClaw, roughly 85% of this surface is irrelevant — handled by other commandclaw services or genuinely out of scope.

The remaining 15% is not a small feature set. Evaluated against CommandClaw's enterprise target state, the gateway requires approximately 100+ discrete features across dual-format API proxying, provider routing/reliability, multi-tenant credential management, guardrails, cost governance, and observability. These features are well-understood, well-documented, and straightforward to implement on top of CommandClaw's existing infrastructure.

Three architectural principles guide the build:

1. **The virtual key system is the gateway's core.** Just as phantom tokens are the core of commandclaw-mcp, virtual keys are the identity primitive that all other features depend on — budgets, rate limiting, cost attribution, guardrail assignment, team membership, and observability labels all key off the virtual key.

2. **The v1 schema must support enterprise features without refactoring.** Team/org columns, hierarchical budget chains, pluggable auth middleware, and guardrail hook points must exist in v1's data model and middleware chain — even if the features themselves ship in v1.1/v1.2.

3. **The gateway is one service in the commandclaw ecosystem, not a platform.** LiteLLM tried to absorb everything adjacent (MCP, A2A, RAG, evals). CommandClaw's gateway handles LLM routing. commandclaw-mcp handles tools. commandclaw-observe handles observability infrastructure. Each service does one thing.

Building a standalone gateway eliminates the LiteLLM vulnerability concern entirely, reduces the dependency footprint to standard Python libraries (FastAPI, httpx, tiktoken, prometheus_client, redis), and produces a gateway architecturally coherent with the rest of the CommandClaw ecosystem.

**The full enterprise-ready gateway is ~14 weeks for one engineer (~3.5 months)** — roughly 25% of the effort LiteLLM has invested in equivalent features, with zero dependency surface, no vulnerability exposure, and a clean separation of concerns across the commandclaw ecosystem.

---

## References

See [litellm-feature-inventory-references.md](litellm-feature-inventory-references.md) for the full bibliography (37 sources).
