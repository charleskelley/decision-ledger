# Pipeline

The runtime decision path from event ingestion to final action, with component contracts, latency budget, and fallback behavior.

---

## Decision Path Overview

```
Event Stream (Redis Streams)
    ↓
[C3] Idempotent Ingestion
     dedup via idempotency key, bounded lateness, dead-letter routing
    ↓
[C4] Online Feature Computation
     sliding window counts, velocity, novelty, geographic anomaly
    ↓
[C5] Fast ML Scorer
     XGBoost risk score + top-k SHAP signals  [target: <10ms P95]
    ↓
[Assembler] Reasoner Assembler  ← domain → framework boundary
     build_observation(LoginEvent, AtoFeatureVector, ScorerOutput) → Observation
     Populates: ReasonerContext + GateContext + FastPathRecord
    ↓  ─── FAST_PATH_ALLOW / FAST_PATH_BLOCK ───→ [C8] Enforcement (fast path)
    ↓  ROUTE_TO_GATE
[C6] Policy RAG Retriever
     pgvector dense + Elasticsearch BM25 → RRF fusion → cross-encoder reranking
    ↓
[C7] LLM Policy Gate
     structured JSON output: permitted_actions, required_controls, rationale, citations
    ↓
[C8] Deterministic Enforcement
     schema validation → rule application → final action + override log
    ↓
[C9] Decision Bundle Construction
     all intermediate states logged, written to PostgreSQL replay store
    ↓
[ALLOW / CHALLENGE / HOLD / BLOCK]
```

---

## Component Contracts

### C3 — Idempotent Ingestion

**Input:** Raw event from producer (scenario generator or live source), pushed to Redis Streams.

**Processing:**
- Computes idempotency key: `hash(user_id + device_id + event_type + timestamp_bucket)`
- Checks key against dedup store (Redis SET with TTL)
- Events older than configurable bounded-lateness window → dead-letter queue
- Schema-validates the event against `LoginEvent` before acknowledging

**Output:** Validated, deduplicated `LoginEvent` on consumer group, ready for feature computation.

**Error handling:** Schema validation failure → dead-letter queue with structured error log. Duplicate events → acknowledged and dropped (idempotent by design). Late events → dead-letter with `BOUNDED_LATENESS` tag.

---

### C4 — Online Feature Computation

**Input:** Validated `LoginEvent` from ingestion consumer group.

**Processing:** Sliding window feature computation over configurable time horizons (1min, 5min, 1hr, 24hr) using Redis sorted sets keyed by `user_id` and `device_id`:

| Feature | Description | Window |
|---------|-------------|--------|
| `velocity_Nmin` | Event count per user in last N minutes | 1, 5, 60, 1440 |
| `ip_novelty` | Is this IP new for this user? (vs. 30-day history) | — |
| `device_novelty` | Is this device fingerprint new for this user? | — |
| `geo_novelty` | Is this country/city new for this user? | — |
| `impossible_travel` | Speed between last and current location exceeds physical limit | — |
| `device_consistency` | Fingerprint match score vs. historical mean | — |

**Output:** `AtoFeatureVector` — named feature values with computation timestamps and window specs.

**Error handling:** Redis unavailability → raise `FeatureComputationError`. Partial window data (new user) → compute available features, set `sparse_history=True` flag. Never silently drop features.

---

### C5 — Fast ML Scorer

**Input:** `FeatureVector` from feature computation.

**Processing:** XGBoost inference over the feature vector. Produces risk score and SHAP-attributed top-k contributing signals.

**Output:** `ScorerOutput` — `risk_score: float [0.0–1.0]`, `top_signals: list[Signal]` (feature name + SHAP value), `scorer_version: str`, `inference_latency_ms: float`.

**Routing logic:**
- `risk_score < LOW_CONFIDENCE_THRESHOLD` → skip LLM gate, route directly to enforcement with `ALLOW` recommendation
- `risk_score > HIGH_CONFIDENCE_THRESHOLD` → skip LLM gate, route to enforcement with `BLOCK` recommendation
- Otherwise → continue to Policy RAG Retriever and LLM Policy Gate

**Target latency:** <10ms P95 for XGBoost inference.

**Error handling:** Model load failure → raise `ScorerInitError` at startup (fail fast). Inference error on a single event → raise `ScorerInferenceError`, route event to `HOLD`.

---

### Reasoner Assembler — Domain → Framework Handoff

**Input:** `LoginEvent` + `AtoFeatureVector` + `ScorerOutput`.

**Processing:** Translates three domain artifacts into the framework's `Observation` contract. This is `build_observation()` in `reasoner/account_takeover/assembler.py` — the only function that crosses the domain → framework boundary.

Populates:
- `ReasonerContext` — model version, inference latency, risk score label, full feature snapshot, SHAP attribution
- `GateContext` — `prompt_template_id`, jurisdiction and risk_tier filters (for retrieval), `template_vars` (pre-rendered strings for the prompt)
- `FastPathRecord` — provenance record for fast-path decisions (routing, threshold rule, feature hash); `None` on `ROUTE_TO_GATE`

**Output:** `LoginEvent` satisfying the `Observation` protocol, with `routing`, `reasoner_context`, `gate_context`, and `fast_path_record` populated.

**Routing thresholds:**
- `FAST_PATH_ALLOW`: `risk_score < 0.20`
- `FAST_PATH_BLOCK`: `risk_score > 0.85`
- `ROUTE_TO_GATE`: otherwise

**Error handling:** Any failure in context assembly → raise `AssemblerError`, route event to `HOLD`.

See [`docs/design/reasoner-handoff.md`](./reasoner-handoff.md) for the full field-level contract.

---

### C6 — Policy RAG Retriever

**Input:** `GateContext` from the assembled `Observation` — specifically `jurisdictions` and `risk_tier` for retrieval filtering, and the retrieval query is constructed from `template_vars` signals (e.g., velocity, device novelty, geo anomaly keywords).

**Processing — Hybrid Retrieval:**

1. **Query construction:** Build retrieval query from top scorer signals and event context (e.g., "velocity spike new device geographic anomaly").
2. **Dense retrieval:** pgvector HNSW similarity search against embedded policy corpus. Returns top-k candidates with cosine similarity scores.
3. **Sparse retrieval:** Elasticsearch BM25 search. Returns top-k candidates with BM25 scores. Metadata filters applied: `jurisdiction`, `version` (prefer latest unless query scopes earlier version).
4. **RRF fusion:** Reciprocal Rank Fusion merges dense and sparse result lists into a unified ranked list.
5. **Cross-encoder reranking:** Reranker scores each (query, candidate) pair. **Latency-budget bypass:** if reranking inference exceeds `rerank_timeout_ms` (configurable), fall back to RRF-only results. Log which path was taken in the Decision Bundle.

**Output:** `RetrievalResult` — top-k `PolicySnippet` objects, each with: `policy_id`, `title`, `version`, `jurisdiction`, `section_path`, `text`, `relevance_score`, `retrieval_path` (reranked or RRF-only).

**Error handling:** pgvector unavailable → raise `RetrievalError`, route to `HOLD`. Elasticsearch unavailable → degrade to dense-only (log degradation). No relevant results found → return empty list, signal to policy gate.

---

### C7 — LLM Policy Gate

**Input:** `GateContext` (from the assembled `Observation`) + `list[PolicySnippet]` (from C6).

**Processing:** Resolves the versioned YAML prompt template via `YamlPromptRegistry` using `gate_context.prompt_template_id`. Renders the template with `gate_context.template_vars` (pre-rendered domain strings: risk score, auth method, travel speed, etc.) and the retrieved policy snippets. The gate never reads domain field names directly — all domain context arrives pre-rendered in `template_vars`.

Calls LLM API (OpenAI GPT-4o or equivalent) with structured output mode. Parses and validates response against `PolicyGateOutput` Pydantic schema.

**Output:** `PolicyGateOutput`:

```python
class PolicyGateOutput(BaseModel):
    permitted_actions: list[DecisionAction]
    required_controls: list[str]
    rationale: str                    # max 200 words
    citations: list[Citation]         # policy_id, snippet, relevance
    confidence: float                 # [0.0–1.0]
    escalate_to_human: bool
    escalation_reason: str | None
    prompt_version: str               # recorded in every bundle
```

**Schema failure handling:** Pydantic validation failure → raise `PolicyGateOutputError`, log the raw LLM response, route event to `HOLD`. Never silently pass an unvalidated output to enforcement.

**Prompt versioning:** Prompt files are immutable once created. A change to prompt behavior requires a new version file (`v2.yaml`, `v3.yaml`, etc.). The active prompt version is recorded in every Decision Bundle. CI blocks merges that activate a new prompt version without a passing eval gate run against that version.

---

### C8 — Deterministic Enforcement

**Input:** `PolicyGateOutput` (or direct scorer routing for high-confidence events).

**Processing — Rule-Based Routing Triggers:**

| Trigger | Condition | Result |
|---------|-----------|--------|
| Schema failure | `PolicyGateOutput` failed Pydantic validation | `HOLD` |
| Novel entity | Fewer than N historical events for this account | `HOLD` with review packet |
| Adversarial flag | Preprocessing detected injection attempt | `BLOCK` |
| Low confidence + high-risk action | `confidence < threshold` AND action is `BLOCK` or `HOLD` | `HOLD` |
| Jurisdiction conflict | Retrieved policy includes conflicting guidance | `HOLD` with conflict detail |

If no trigger fires, the enforcement layer applies the `permitted_actions` from the policy gate output, resolving to the most conservative permissible action.

**Output:** `EnforcementDecision` — `final_action: DecisionAction`, `enforcement_rule_applied: str | None`, `override_log: list[str]`, `review_packet: ReviewPacket | None`.

**Invariant:** Enforcement is pure rule-based logic with no LLM calls. Every execution path is deterministic given the same inputs. This is what makes replay work.

---

### C9 — Decision Bundle Construction

Every decision produces a complete `DecisionBundle`, written to the PostgreSQL audit store. See [Data — Decision Bundle](./data.md#decision-bundle) for the full schema.

**Replay:** Given a bundle ID, the replay command loads all logged intermediate states and re-executes `enforcement.resolve()` against them. It does not re-invoke the LLM — the logged LLM output is fed directly to enforcement. The replay guarantee is: identical enforcement inputs → identical final action.

---

## Latency Budget

<!-- TODO: Fill in with measured P50/P95 numbers from Week 5 build. -->
<!-- Document per-component allocation and total pipeline budget. -->
<!-- Key decision: LLM inference dominates. Document tradeoffs considered: -->
<!-- smaller model, reduced context, async gate, streaming output. -->

| Component | Target P95 | Notes |
|-----------|-----------|-------|
| Ingestion + dedup | <5ms | Redis SET check |
| Feature computation | <15ms | Redis sorted set reads |
| ML scoring | <10ms | XGBoost inference |
| Policy retrieval | <100ms | Hybrid search + reranking (with bypass) |
| LLM policy gate | TBD | Dominates; depends on model and context length |
| Enforcement | <5ms | Pure rule evaluation |
| Bundle write | <20ms | PostgreSQL insert |
| **Total (fast path)** | **<55ms** | No LLM invocation |
| **Total (full path)** | **TBD** | LLM latency dominates |

_Actual measured numbers will be filled in during Week 5._

---

## Fallback Behavior

The system has explicit fallback behavior at every failure point. The invariant: **any failure routes to `HOLD`, never to silent enforcement**.

| Failure | Fallback |
|---------|----------|
| Feature computation error | `HOLD` + error log |
| Scorer inference error | `HOLD` + error log |
| Retrieval unavailable | `HOLD` + degraded log |
| LLM API error / timeout | `HOLD` + error log |
| Schema validation failure | `HOLD` + raw LLM output logged |
| Enforcement rule error | `HOLD` + error log |

Cross-encoder reranking is the exception: it degrades to RRF-only results (not `HOLD`) because retrieval without reranking is still meaningful retrieval.
