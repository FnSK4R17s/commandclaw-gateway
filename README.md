<p align="center">
  <img src="logo.png" alt="Command Claw Gateway" height="88">
</p>

<h1 align="center">Command Claw Gateway</h1>

<p align="center">
  <strong>The LLM routing layer for CommandClaw agents.</strong><br>
  <em>FastAPI service that holds provider credentials, issues virtual keys, enforces budgets and rate limits, routes across providers with fallbacks, and tracks every token and dollar.</em><br>
  <sub>One interface, every LLM. No LiteLLM dependency.</sub>
</p>

---

> Have feedback or found a bug? Reach out at [**@_Shikh4r_** on X](https://x.com/_Shikh4r_)

## What this is

`commandclaw-gateway` is a standalone LLM gateway built for the CommandClaw enterprise agent platform. It sits between every agent and every LLM provider, serving both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) wire formats from a single service.

Agents never hold real provider credentials. They hold virtual keys — scoped tokens with budgets, rate limits, model allowlists, and team membership. The gateway validates the key, enforces all constraints, routes to the best available deployment, and tracks every token, every dollar, every latency metric.

The gateway is **one service in the CommandClaw ecosystem, not a platform**. It routes LLM traffic. commandclaw-mcp handles tools. commandclaw-observe handles observability infrastructure. Each service does one thing.

```python
# Before gateway — agent holds real credentials
llm = ChatOpenAI(base_url="https://api.openai.com/v1", api_key="sk-real-key", model="gpt-4o")

# After gateway — agent holds virtual key, gateway holds real credentials
llm = ChatOpenAI(base_url="http://commandclaw-gateway:4000/v1", api_key="sk-cc-virtual-key", model="gpt-4o")
```

## Features

| Capability | Status | Details |
|---|---|---|
| **Dual-format API** | Implemented | OpenAI `/v1/chat/completions` + Anthropic `/v1/messages` — both first-class, streaming + non-streaming |
| **Provider adapters** | Implemented | OpenAI, Anthropic, Vertex AI, Bedrock, Ollama. OpenAI-compat baseline for Groq, DeepSeek, etc. |
| **Virtual keys** | Implemented | Generate, rotate (grace period), block/unblock, delete. Per-key budgets, rate limits, model allowlists, team/org association. |
| **Routing** | Implemented | Filter pipeline (cooldown, region, context window, upstream awareness) + strategy (shuffle, least-busy, latency-based, cost-based, custom plugin) |
| **Fallbacks** | Implemented | Standard, context window overflow, content policy. Configurable fallback chains per model. |
| **Retries** | Implemented | Exponential backoff with jitter, `Retry-After` header compliance, per-exception `RetryPolicy` |
| **Cooldowns** | Implemented | Per-deployment failure tracking, per-exception `AllowedFailsPolicy` thresholds |
| **Cost tracking** | Implemented | tiktoken counting, YAML pricing table, hierarchical spend accumulation (org > team > user > key), spend tags, spend logs |
| **Rate limiting** | Implemented | Multi-dimensional Redis sliding windows + token bucket. Per-key, per-key-per-model, per-user, per-team, per-org, per-model-global. RPM + TPM + daily/monthly quotas. |
| **Caching** | Implemented | Redis exact-match + in-memory backend. Streaming assembly, per-model TTL, cache invalidation (model index), multi-tenant isolation (`cache_scope`), `supported_call_types` enforcement |
| **Guardrails** | Implemented | PII detection/redaction (Presidio + builtin), prompt injection detection, generic guardrail API, per-key assignment, execution tracing in Langfuse |
| **Auth** | Implemented | Virtual keys + JWT/OIDC (JWKS + symmetric). Pluggable auth middleware. |
| **RBAC** | Implemented | `proxy_admin`, `team_admin`, `internal_user`. Key generation bounds. |
| **Teams** | Implemented | Create, update, delete. Team members, budgets, rate limits, model allowlists, guardrail policies, region constraints. |
| **Organizations** | Implemented | Create, update, delete. Org-level budgets and rate limits. |
| **Audit trail** | Implemented | Immutable append-only log of key/team/org operations. Queryable by resource, actor, action. |
| **Batch inference** | Implemented | `POST /v1/batches` — background batch processing with status tracking |
| **Responses API** | Implemented | `POST /v1/responses` — OpenAI Responses API format, translates to/from chat completions |
| **Observability** | Implemented | ~20 Prometheus metrics (all carry team/org/key/model labels), Langfuse tracing, Slack alerts, callback monitoring |
| **Health checks** | Implemented | Liveness, readiness, background deployment probing, deployment state gauges |
| **Spend reporting** | Implemented | `GET /global/spend/report` (group by key/user/team/model/tag), `GET /global/spend/daily`, `POST /global/spend/reset` |

## Architecture

```
   Agent (LangGraph)                   commandclaw-gateway (FastAPI :4000)
   │                                   │
   ├─ ChatOpenAI(base_url=...) ──►     ├─ Auth middleware (virtual key + JWT/OIDC)
   │   /v1/chat/completions            ├─ RBAC enforcement
   │                                   ├─ Rate limiter (multi-dimensional sliding window + token bucket)
   ├─ Anthropic(base_url=...) ──►      ├─ Budget check (hierarchical: org > team > user > key)
   │   /v1/messages                    ├─ Cache (Redis exact / in-memory, multi-tenant isolation)
   │                                   ├─ Guardrail chain (PII, injection, generic API)
   ├─ Embeddings ──────────────►       ├─ Router (region → cooldown → upstream → context window → strategy)
   │   /v1/embeddings                  ├─ Provider adapters (OpenAI, Anthropic, Vertex, Bedrock, Ollama)
   │                                   ├─ Reliability engine (fallbacks × 3, retries, cooldowns)
   ├─ Batch ───────────────────►       ├─ Cost tracker (token counting, spend accumulation, spend tags)
   │   /v1/batches                     ├─ Audit trail (immutable operation log)
   │                                   └─ Observability (Prometheus, Langfuse, Slack)
   └─ Responses ───────────────►           │
       /v1/responses                       ├──► Redis (cache, rate limits, spend, keys, teams, orgs)
                                           ├──► Langfuse (traces, guardrail audit)
                                           ├──► Prometheus (metrics)
                                           └──► LLM Providers (OpenAI, Anthropic, Vertex, Bedrock, Ollama)
```

## Quickstart

### Prerequisites

- Python 3.12+
- Redis 7+

### Local development

```bash
# Clone and install
git clone https://github.com/FnSK4R17s/commandclaw-gateway.git
cd commandclaw-gateway
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your provider keys, Redis host, etc.

# Run
uvicorn main:app --host 0.0.0.0 --port 4000 --reload
```

### Docker Compose

```bash
docker compose up -d
```

This starts the gateway on port 4000 with a Redis instance.

### Generate a virtual key

```bash
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $GATEWAY_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "agent-1",
    "models": ["gpt-4o", "claude-sonnet"],
    "max_budget": 100.0,
    "budget_duration": "daily",
    "rpm_limit": 100,
    "tpm_limit": 100000
  }'
```

### Use the virtual key

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000/v1",
    api_key="sk-cc-<your-virtual-key>",
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello!"}],
)
```

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://localhost:4000",
    api_key="sk-cc-<your-virtual-key>",
)

response = client.messages.create(
    model="claude-sonnet",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

## API Endpoints

### LLM Proxy

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI chat completions (streaming + non-streaming) |
| `POST` | `/v1/messages` | Anthropic Messages API |
| `POST` | `/v1/messages/count_tokens` | Anthropic token counting |
| `POST` | `/v1/embeddings` | Embedding proxy |
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/batches` | Batch inference |
| `GET` | `/v1/batches/{batch_id}` | Batch status |
| `POST` | `/v1/responses` | OpenAI Responses API |

### Key Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/key/generate` | Create virtual key |
| `GET` | `/key/info?key_id=` | Key details + spend |
| `GET` | `/key/list` | List keys |
| `POST` | `/key/block?key_id=` | Instant revocation |
| `POST` | `/key/unblock?key_id=` | Unblock |
| `DELETE` | `/key/{key_id}` | Delete (spend logs retained) |
| `POST` | `/key/{key_id}/regenerate` | Rotate with grace period |
| `PATCH` | `/key/{key_id}` | Update constraints |

### User / Team / Org Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/user/new` | Create user |
| `GET` | `/user/info?user_id=` | User details + spend |
| `POST` | `/team/new` | Create team |
| `GET` | `/team/info?team_id=` | Team details |
| `PATCH` | `/team/{team_id}` | Update team |
| `POST` | `/team/{team_id}/member` | Add member |
| `POST` | `/org/new` | Create organization |
| `GET` | `/org/info?org_id=` | Org details |
| `PATCH` | `/org/{org_id}` | Update org |

### Spend & Cache

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/spend/logs` | Query spend logs (filter by key, user, tag) |
| `GET` | `/global/spend/report` | Aggregated report (group by key/user/team/model/tag) |
| `GET` | `/global/spend/daily` | Daily activity breakdown |
| `POST` | `/global/spend/reset` | Reset all spend counters |
| `GET` | `/cache/ping` | Cache health |
| `DELETE` | `/cache/delete` | Invalidate by model or key |
| `DELETE` | `/cache/flush` | Flush entire cache |

### Audit & Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/audit/logs` | Query audit trail |
| `GET` | `/health` | Overall status |
| `GET` | `/health/readiness` | Redis connectivity |
| `GET` | `/health/liveliness` | Process alive |
| `GET` | `/metrics` | Prometheus metrics |

## Configuration

The gateway reads `config.yaml` at startup. Environment variables substitute via `os.environ/VAR_NAME` syntax.

```yaml
model_list:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
      api_base: https://api.openai.com/v1
      rpm: 500
      tpm: 100000

router_settings:
  routing_strategy: simple-shuffle  # simple-shuffle | least-busy | latency-based | cost-based | custom
  num_retries: 3
  timeout: 30
  allowed_fails: 3
  cooldown_time: 60
  fallbacks:
    gpt-4o: [claude-sonnet, gpt-4o-mini]

cache_params:
  type: redis          # redis | memory | none
  default_ttl: 86400
  cache_scope: team    # global | team | key

general_settings:
  master_key: os.environ/GATEWAY_MASTER_KEY
  auth_strategy: api_key  # api_key | jwt
```

Model pricing is defined in `pricing.yaml` (cost per 1K tokens).

## Project Structure

```
commandclaw-gateway/
  main.py                          # FastAPI app, route registration, lifespan
  config.py                        # YAML config loader, env var substitution
  pricing.yaml                     # Model pricing table

  auth/
    middleware.py                   # Auth middleware (API key + JWT/OIDC)
    virtual_keys.py                # Key lifecycle (generate, validate, rotate, block)
    budgets.py                     # Hierarchical budget enforcement + spend tracking
    jwt_auth.py                    # JWT/OIDC validation (JWKS + symmetric)
    teams.py                       # Team and organization CRUD
    rbac.py                        # Role-based access control
    audit.py                       # Immutable audit trail

  providers/
    base.py                        # BaseLLMProvider ABC
    openai_provider.py             # OpenAI adapter (baseline for OpenAI-compat)
    anthropic_provider.py          # Anthropic Messages API adapter
    vertex_provider.py             # Google Vertex AI / Gemini adapter
    bedrock_provider.py            # AWS Bedrock adapter (boto3 SigV4)
    ollama_provider.py             # Ollama (OpenAI-compat subclass)

  routing/
    router.py                      # Filter pipeline + strategy dispatch
    strategies.py                  # Custom plugin ABC, region filter, latency/cost routing,
                                   #   canary splits, context window check, token bucket,
                                   #   traffic mirroring, priority tiers
    fallbacks.py                   # Fallback chains (standard, context window, content policy)
    retries.py                     # Retry logic, RetryPolicy, AllowedFailsPolicy
    cooldowns.py                   # Deployment cooldown tracking

  middleware/
    rate_limiter.py                # Multi-dimensional sliding window + token bucket
    cache.py                       # Redis exact cache, streaming assembly, multi-tenant isolation
    memory_cache.py                # In-memory LRU cache backend
    cost_tracker.py                # Token counting, spend accumulation, spend logs
    guardrails.py                  # PII detection, prompt injection, generic API

  observability/
    metrics.py                     # ~20 Prometheus metrics
    callbacks.py                   # Langfuse tracing, Slack alerts, guardrail audit logging
    health.py                      # Background deployment health probing

  routes/
    chat.py                        # POST /v1/chat/completions
    messages.py                    # POST /v1/messages
    embeddings.py                  # POST /v1/embeddings
    models.py                      # GET /v1/models
    health.py                      # Health + metrics endpoints
    keys.py                        # Virtual key management
    users.py                       # User management
    teams.py                       # Team management
    orgs.py                        # Organization management
    audit.py                       # Audit log queries
    spend.py                       # Spend logs + cache management
    global_spend.py                # Global spend reporting
    batches.py                     # POST /v1/batches
    responses.py                   # POST /v1/responses

  schemas/
    common.py                      # IdentityContext, Deployment, SpendLog, etc.
    openai.py                      # OpenAI wire format models
    anthropic.py                   # Anthropic wire format models

  infra/
    redis_client.py                # Redis async client + key pattern docs
    encryption.py                  # Fernet encryption for credentials at rest
    token_counter.py               # tiktoken + fallback counting
    cost_calculator.py             # Pricing table lookup

  tests/                           # 82 tests
```

## Response Headers

Every response includes gateway-specific headers for transparency:

| Header | Description |
|--------|-------------|
| `x-litellm-response-cost` | USD cost of this request (0.0 on cache hit) |
| `x-gateway-request-id` | Unique request ID for trace correlation |
| `x-gateway-cache-key` | Cache key hash (for debugging) |
| `x-gateway-model` | Deployment ID that served the request |
| `x-ratelimit-limit-requests` | RPM limit |
| `x-ratelimit-remaining-requests` | Remaining RPM |
| `x-ratelimit-limit-tokens` | TPM limit |
| `x-ratelimit-remaining-tokens` | Remaining TPM |
| `retry-after` | Seconds until rate limit resets (on 429) |

## Tests

```bash
python -m pytest tests/ -v
```

82 tests covering cache key correctness, provider transforms, routing logic, cost calculation, RBAC, guardrails, multi-tenant cache isolation, retry policies, and the Responses API.

## Related repos

| Repo | Purpose |
|------|---------|
| [commandclaw](https://github.com/FnSK4R17s/commandclaw) | Agent runtime, Telegram I/O, tracing |
| [commandclaw-mcp](https://github.com/FnSK4R17s/commandclaw-mcp) | MCP gateway — credential proxy with rotating keys |
| [commandclaw-observe](https://github.com/FnSK4R17s/commandclaw-observe) | Observability stack (Langfuse, Prometheus, Grafana) |
| [commandclaw-memory](https://github.com/FnSK4R17s/commandclaw-memory) | Memory service — wiki, retrieval, distillation |
| [commandclaw-wiki](https://github.com/FnSK4R17s/commandclaw-wiki) | Shared knowledge base |
| [commandclaw-skills](https://github.com/FnSK4R17s/commandclaw-skills) | Skills library |
| [commandclaw-vault](https://github.com/FnSK4R17s/commandclaw-vault) | Per-agent vault template |

## License

MIT
