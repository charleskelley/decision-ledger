# Data

Schemas, corpus model, and bundle structure for the ATO Reasoner. This document covers the three primary data artifacts: the login event schema, the policy corpus model, and the Decision Bundle.

---

## Login Event Schema

Every event flowing through the pipeline conforms to `LoginEvent`. The `scenario_tag` field is stripped before scoring — it exists only for evaluation and replay tooling.

```python
class LoginEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    # Identity
    event_id: str           # UUID — idempotency key
    account_id: str
    session_id: str
    timestamp: datetime

    # Network
    ip_address: str         # IPv4 or IPv6
    geo: GeoData            # lat/lon, country, city, asn

    # Device
    device_fingerprint: str
    user_agent: str

    # Auth context
    auth_method: AuthMethod  # PASSWORD, MFA_TOTP, MFA_PUSH, SSO, etc.
    outcome: AuthOutcome     # SUCCESS, FAILURE, TIMEOUT, BLOCKED

    # Generator only — stripped before scoring
    scenario_tag: str | None = None
```

```python
class GeoData(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    lat: float
    lon: float
    country: str            # ISO 3166-1 alpha-2
    city: str
    asn: str                # Autonomous System Number
```

### Idempotency Key

The deduplication key is computed as:

```
SHA-256(account_id + ":" + device_fingerprint + ":" + auth_method + ":" + timestamp_bucket)
```

Where `timestamp_bucket` rounds the timestamp to the nearest configurable window (default: 1 minute). This ensures that a retry of the same event within the window is treated as a duplicate, while a legitimately different event in the same minute (different device, different auth method) is not.

---

## Feature Vector

Computed by the online feature layer (`app/features/`) from the raw event and the user's
sliding window state in Redis. This is an ATO Reasoner domain type — it lives in
`reasoner/account_takeover/features.py`, not in `core/`.

```python
class AtoFeatureVector(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    account_id: str
    computed_at: datetime
    sparse_history: bool    # True if account has < N historical events

    # Velocity features
    velocity_1min: int      # Events in last 1 minute for this account
    velocity_5min: int
    velocity_60min: int
    velocity_1440min: int   # 24-hour window

    # Novelty features
    ip_novelty: float       # [0.0–1.0] — 0 = known IP, 1 = never seen before
    device_novelty: float
    geo_novelty: float      # Country-level novelty for this account

    # Geographic signals
    impossible_travel: bool
    travel_speed_kmh: float | None  # None if no previous location on record

    # Device signals
    device_consistency_score: float  # Match vs. historical fingerprint mean
    user_agent_consistency: float

    # Window metadata
    windows: list[WindowSpec]  # Which windows were available (sparse history may limit)
```

---

## Policy Corpus Model

The policy corpus is a versioned collection of 30–50 documents. Each document is chunked hierarchically and stored with metadata that the retriever uses for filtering and ranking.

### Document Metadata Schema

```python
class PolicyDocument(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    policy_id: str          # e.g., "NIST-800-63B", "INTERNAL-RISK-v2.1"
    title: str
    version: str            # Semantic version — "2.1", "4.0"
    jurisdiction: Jurisdiction  # US_FEDERAL, US_STATE, EU_GDPR, INTERNAL
    effective_date: date
    supersedes: str | None  # policy_id of the document this replaces
    risk_tier: RiskTier | None  # STANDARD, HIGH_VALUE, ENTERPRISE
    document_type: DocumentType # REGULATION, GUIDANCE, INTERNAL_POLICY, STANDARD
```

### Chunking Strategy

Policy documents are chunked hierarchically, preserving document structure:

```
Document
└── Section (e.g., "5. Authenticator Lifecycle Management")
    └── Subsection (e.g., "5.2.3 — Authenticator Assurance Level 2")
        └── Paragraph (smallest retrieval unit)
```

Every chunk stores its parent section header as metadata. This enables the retriever to surface a specific paragraph while providing the section header as context — the paragraph is the retrieval unit, the section is the context window.

### Corpus Sources

**Real public sources (actual text):**
- NIST SP 800-63B — Digital Identity Guidelines: Authentication and Lifecycle Management
- FFIEC Authentication in an Internet Banking Environment (2005 + 2011 supplement)
- PCI-DSS v4.0 — Requirement 8: Identify Users and Authenticate Access to System Components
- OWASP Authentication Cheat Sheet

**Synthetic internal documents (deliberate complexity):**
- `INTERNAL-RISK-v1.0` and `INTERNAL-RISK-v2.1` — conflicting velocity thresholds (v2.1 supersedes v1.0; retrieval must prefer latest unless query explicitly scopes v1.0)
- US vs. EU jurisdiction variants — GDPR data minimization obligations create conflict with US retention requirements; the policy gate must surface and flag this conflict
- `HIGH-VALUE-ACCOUNT-ADDENDUM` — overrides base risk policy for accounts above a configurable threshold; retrieval must apply this when the `risk_tier` metadata matches

---

## Scorer Output

`ScorerOutput` is a domain type — it lives in `reasoner/account_takeover/scorer.py`.
The assembler translates it into `ReasonerContext` and `FastPathRecord` before the
framework sees it. `Signal` and `FastPathRecord` are framework types in `core/decision/routing.py`.

```python
class Signal(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    feature_name: str
    shap_value: float
    raw_value: float

class ScorerOutput(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    account_id: str
    risk_score: float           # [0.0–1.0]
    top_signals: list[Signal]   # Top-k by absolute SHAP value
    scorer_version: str
    inference_latency_ms: float
    routing: GateRouting        # FAST_PATH_ALLOW | FAST_PATH_BLOCK | ROUTE_TO_GATE
```

---

## Policy Gate Output

```python
class Citation(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    policy_id: str
    snippet: str
    relevance: str      # Human-readable relevance explanation

class PolicyGateOutput(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    permitted_actions: list[DecisionAction]
    required_controls: list[str]
    rationale: str              # max 200 words
    citations: list[Citation]
    confidence: float           # [0.0–1.0]
    escalate_to_human: bool
    escalation_reason: str | None
    # prompt_template_id is recorded in GateContext (raw_event.gate_context),
    # not here — it is an audit key on the Observation, not a gate output field.
```

---

## Decision Bundle

The complete audit record for every decision. Stored in PostgreSQL, indexed by `decision_id`. Every field is logged at decision time — nothing is computed on read.

Per DR-14, the bundle no longer has separate top-level fields for `feature_snapshot`,
`scorer_output`, or `model_version`. All domain context lives inside `raw_event` (the
`Observation`): ML evidence in `raw_event.reasoner_context`, retrieval and prompt context
in `raw_event.gate_context`, and fast-path provenance in `raw_event.fast_path_record`.
This makes `DecisionBundle` domain-agnostic — it stores an `Observation` and the
framework's processing outputs, nothing ATO-specific.

```python
class DecisionBundle(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    # Identity
    decision_id: str            # UUID
    created_at: datetime

    # Input layer
    # raw_event is a LoginEvent satisfying the Observation protocol.
    # It carries: reasoner_context (ML evidence + feature snapshot),
    # gate_context (prompt_template_id, jurisdictions, risk_tier, template_vars),
    # and fast_path_record (fast-path provenance; None on ROUTE_TO_GATE).
    raw_event: Observation
    idempotency_key: str
    ingestion_timestamp: datetime

    # Retrieval layer — None on fast path (retrieval is skipped)
    retrieval_query: str | None
    retrieval_results: list[PolicySnippet]
    retrieval_path: str | None  # "reranked" | "rrf_only" | None

    # Gate layer — None on fast path (LLM is not invoked)
    rendered_prompt: str | None         # Full prompt as sent to the LLM
    raw_llm_response: str | None        # Raw string response from LLM API
    policy_gate_output: PolicyGateOutput | None  # None if schema validation failed or fast path

    # Decision layer
    final_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]
    review_packet: ReviewPacket | None

    # Corpus version
    policy_corpus_version: str          # Version tag of the loaded corpus at decision time

    # Telemetry
    latency_breakdown: dict[str, float]  # component → duration_ms
    llm_token_cost: TokenCost | None     # None on fast path
```

### Replay fields

The replay guarantee relies on fields stored inside `raw_event`:

| Replay requirement | Location in bundle |
|---|---|
| Full feature snapshot | `raw_event.reasoner_context.feature_set` |
| Model version | `raw_event.reasoner_context.model_version` |
| Fast-path threshold rule | `raw_event.fast_path_record.threshold_rule` |
| Feature hash (drift detection) | `raw_event.fast_path_record.feature_hash` |
| Prompt template used | `raw_event.gate_context.prompt_template_id` |
| Rendered template vars | `raw_event.gate_context.template_vars` |
| Cached LLM output (gate path) | `rendered_prompt` + `raw_llm_response` + `policy_gate_output` |

### Replay Contract

The replay guarantee: given a `DecisionBundle`, re-executing `enforcement.resolve()` against the logged `policy_gate_output` and `feature_snapshot` produces the same `final_action`.

Replay does **not** re-invoke the LLM. The `raw_llm_response` and `policy_gate_output` are already in the bundle. Replay feeds the cached output back through enforcement — the same deterministic rule evaluation that ran at decision time. This is what "deterministic replay" means: verifying enforcement determinism, not LLM reproducibility.

```bash
# Replay a single decision
uv run python -m decision_ledger.audit replay --id <decision_id>

# Diff two decisions
uv run python -m decision_ledger.audit diff --id <id_a> --id <id_b>

# CI replay check (20 random bundles, byte-identical assertion)
make test-replay
```

---

## Action Space

```python
class DecisionAction(str, Enum):
    ALLOW = "ALLOW"
    CHALLENGE = "CHALLENGE"
    HOLD = "HOLD"
    BLOCK = "BLOCK"
```

| Action | Meaning |
|--------|---------|
| `ALLOW` | Request proceeds without friction |
| `CHALLENGE` | MFA or step-up authentication required |
| `HOLD` | Request queued for human review; no immediate action |
| `BLOCK` | Request denied; session terminated |

Action severity is ordered: `ALLOW < CHALLENGE < HOLD < BLOCK`. The enforcement layer resolves to the most conservative permissible action when the policy gate returns multiple permitted actions.
