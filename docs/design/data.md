# Data

Schemas, corpus model, and bundle structure for the ATO Reasoner. This document
covers the three primary data artifacts: the login event schema, the policy
corpus model, and the Decision Bundle.

---

## Login Event Schema

Every event flowing through the pipeline conforms to `LoginEvent`. The
`scenario_tag` field is stripped before scoring — it exists only for evaluation
and replay tooling.

```python
class LoginEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    # Identity
    event_id: str  # UUID — idempotency key
    account_id: str
    session_id: str
    timestamp: datetime

    # Network
    ip_address: str  # IPv4 or IPv6
    geo: GeoData  # lat/lon, country, city, asn

    # Device
    device_fingerprint: str
    user_agent: str

    # Auth context
    auth_method: AuthMethod  # PASSWORD, MFA_TOTP, MFA_PUSH, SSO, etc.
    outcome: AuthOutcome  # SUCCESS, FAILURE, TIMEOUT, BLOCKED

    # Generator only — stripped before scoring
    scenario_tag: str | None = None
```

```python
class GeoData(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    lat: float
    lon: float
    country: str  # ISO 3166-1 alpha-2
    city: str
    asn: str  # Autonomous System Number
```

### Idempotency Key

The deduplication key is computed as:

```
SHA-256(account_id + ":" + device_fingerprint + ":" + auth_method + ":" + timestamp_bucket)
```

Where `timestamp_bucket` rounds the timestamp to the nearest configurable
window (default: 1 minute). This ensures that a retry of the same event within
the window is treated as a duplicate, while a legitimately different event in
the same minute (different device, different auth method) is not.

---

## Feature Vector

Computed by the online feature layer (
`reasoner/account_takeover/feature_service.py`) from the raw event and the
user's sliding window state in Redis. This is an ATO Reasoner domain type — it
lives in
`reasoner/account_takeover/features.py`, not in `core/`.

```python
class AtoFeatureVector(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    account_id: str
    computed_at: datetime
    sparse_history: bool  # True if account has < N historical events

    # Velocity features
    velocity_1min: int  # Events in last 1 minute for this account
    velocity_5min: int
    velocity_60min: int
    velocity_1440min: int  # 24-hour window

    # Novelty features
    ip_novelty: float  # [0.0–1.0] — 0 = known IP, 1 = never seen before
    device_novelty: float
    geo_novelty: float  # Country-level novelty for this account

    # Geographic signals
    impossible_travel: bool
    travel_speed_kmh: float | None  # None if no previous location on record

    # Device signals
    device_consistency_score: float  # Match vs. historical fingerprint mean
    user_agent_consistency: float

    # Window metadata
    windows: list[
        WindowSpec]  # Which windows were available (sparse history may limit)
```

---

## Policy Corpus Model

The policy corpus is a versioned collection of 30–50 documents. Each document is
chunked hierarchically and stored with metadata that the retriever uses for
filtering and ranking.

### Document Metadata Schema

```python
class PolicyDocument(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    policy_id: str  # e.g., "NIST-800-63B", "INTERNAL-RISK-v2.1"
    title: str
    version: str  # Semantic version — "2.1", "4.0"
    jurisdiction: Jurisdiction  # US_FEDERAL, US_STATE, EU_GDPR, INTERNAL
    effective_date: date
    supersedes: str | None  # policy_id of the document this replaces
    risk_tier: RiskTier | None  # STANDARD, HIGH_VALUE, ENTERPRISE
    document_type: DocumentType  # REGULATION, GUIDANCE, INTERNAL_POLICY, STANDARD
```

### Chunking Strategy

Policy documents are chunked hierarchically, preserving document structure:

```
Document
└── Section (e.g., "5. Authenticator Lifecycle Management")
    └── Subsection (e.g., "5.2.3 — Authenticator Assurance Level 2")
        └── Paragraph (smallest retrieval unit)
```

Every chunk stores its parent section header as metadata. This enables the
retriever to surface a specific paragraph while providing the section header as
context — the paragraph is the retrieval unit, the section is the context
window.

### Corpus Sources

**Real public sources (actual text):**

- NIST SP 800-63B — Digital Identity Guidelines: Authentication and Lifecycle
  Management
- FFIEC Authentication in an Internet Banking Environment (2005 + 2011
  supplement)
- PCI-DSS v4.0 — Requirement 8: Identify Users and Authenticate Access to System
  Components
- OWASP Authentication Cheat Sheet

**Synthetic internal documents (deliberate complexity):**

- `INTERNAL-RISK-v1.0` and `INTERNAL-RISK-v2.1` — conflicting velocity
  thresholds (v2.1 supersedes v1.0; retrieval must prefer latest unless query
  explicitly scopes v1.0)
- US vs. EU jurisdiction variants — GDPR data minimization obligations create
  conflict with US retention requirements; the policy gate must surface and flag
  this conflict
- `HIGH-VALUE-ACCOUNT-ADDENDUM` — overrides base risk policy for accounts above
  a configurable threshold; retrieval must apply this when the `risk_tier`
  metadata matches

---

## Scorer Output

`ScorerOutput` is a domain type — it lives in
`reasoner/account_takeover/scorer.py`. The assembler translates it into
`ReasonerContext` and `FastPathRecord` before the framework sees it. `Signal`
and `FastPathRecord` are framework types in `core/decision/routing.py`.

```python
class Signal(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    feature_name: str
    shap_value: float
    raw_value: float


class ScorerOutput(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    account_id: str
    risk_score: float  # [0.0–1.0]
    top_signals: list[Signal]  # Top-k by absolute SHAP value
    scorer_version: str
    inference_latency_ms: float
    routing: GateRouting  # FAST_PATH_ALLOW | FAST_PATH_BLOCK | ROUTE_TO_GATE
```

---

## Policy Gate Output

```python
class Citation(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    policy_id: str
    snippet: str
    relevance: str  # Human-readable relevance explanation

```

### Gate-invocation contracts (DR-19, DR-20)

Gate-invocation artifacts are captured in two typed contracts on the bundle:
`GateInput` (input side) and `GateOutput` (output side). Both are
`None` on the fast path. Both populated when the gate ran (whether or not
validation succeeded).

Per DR-20, the framework defines **universal base contracts**;
**concrete subclasses live in per-gate-type subpackages** (e.g.,
`core/gate/policy/` for the LLM-backed policy gate). The bundle types its fields
as the universal base; subclass instances are preserved at runtime.

```python
# Universal — core/gate/
class GateVerdict(BaseModel):
    """Enforcement-consumable verdict; carries only what enforcement reads."""
    gate_id: str
    permitted_actions: list[DecisionAction]
    required_controls: list[str]
    confidence: float  # [0.0–1.0]
    escalate_to_human: bool
    escalation_reason: str | None


class GateInput(BaseModel):
    gate_id: str  # discriminator (Literal in subclasses)


class GateOutput(BaseModel):
    gate_id: str
    verdict: GateVerdict | None  # None when validation failed


# LLM-policy concrete — core/gate/policy/
class PolicyGateVerdict(GateVerdict):
    gate_id: Literal["policy"] = "policy"
    rationale: str  # max 200 words
    citations: list[Citation]


class PolicyGateInput(GateInput):
    gate_id: Literal["policy"] = "policy"
    model_version: str
    prompt_template_id: str
    prompt_template_version: str
    corpus_version: str
    rendered_prompt: str
    prompt_snapshot: PromptSnapshot
    template_vars: dict[str, str]


class PolicyGateOutput(GateOutput):
    gate_id: Literal["policy"] = "policy"
    verdict: PolicyGateVerdict | None  # narrowed type
    response_text: str | None = None  # raw LLM output
    token_cost: TokenCost | None = None
```

The bundle's persistence layer (`app/audit/store.py`) uses Pydantic
discriminated-union deserialization (closed union over framework-known gate
kinds) to reconstruct the correct subclass when reading a stored bundle. Adding
a new gate kind extends the union; the bundle's typed surface (
`gate_input: GateInput | None`) does not change. See
[`gates.md`](./gates.md) for the gate-implementation guide.

---

## Decision Bundle

The complete audit record for every decision. Stored in PostgreSQL, indexed by
`decision_id`. Every field is logged at decision time — nothing is computed on
read.

Per DR-14, the bundle no longer has separate top-level fields for
`feature_snapshot`,
`scorer_output`, or `model_version`. All domain context lives inside
`raw_event` (the
`Observation`): ML evidence in `raw_event.reasoner_context`, retrieval and
prompt context in `raw_event.gate_context`, and fast-path provenance in
`raw_event.fast_path_record`. This makes `DecisionBundle` domain-agnostic — it
stores an `Observation` and the framework's processing outputs, nothing
ATO-specific.

```python
class DecisionBundle(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    # Identity
    decision_id: str  # UUID
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
    retrieval_results: list[RetrievedSnippet]
    retrieval_path: str | None  # "reranked" | "rrf_only" | None

    # Gate layer (DR-19) — both None on fast path. Both populated when
    # the gate ran (even if validation failed; in that case
    # gate_output.verdict is None).
    gate_input: GateInput | None
    gate_output: GateOutput | None

    # Decision layer
    decision_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]

    # Telemetry
    latency_breakdown: dict[str, float]  # component → duration_ms
```

### Replay fields

The replay guarantee relies on fields stored inside `raw_event`:

| Replay requirement                 | Location in bundle                                                                                  |
|------------------------------------|-----------------------------------------------------------------------------------------------------|
| Full feature snapshot              | `raw_event.reasoner_context.feature_set`                                                            |
| Model version                      | `raw_event.reasoner_context.model_version`                                                          |
| Fast-path threshold rule           | `raw_event.fast_path_record.threshold_rule`                                                         |
| Feature hash (drift detection)     | `raw_event.fast_path_record.feature_hash`                                                           |
| Prompt template used               | `raw_event.gate_context.prompt_template_id`                                                         |
| Rendered template vars             | `raw_event.gate_context.template_vars`                                                              |
| Cached gate invocation (gate path) | `gate_input` + `gate_output` (with `gate_output.verdict` as the typed payload enforcement consumes) |

### Replay Contract

The replay guarantee: given a `DecisionBundle`, re-executing
`enforcement.resolve()` against the logged `gate_output.verdict` and
`feature_snapshot` produces the same `decision_action`.

### Action vocabulary

The framework distinguishes three concepts (DR-18):

| Term                | Source                  | Meaning                                                                                                                                                                                                                                                            |
|---------------------|-------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `decision_action`   | `DecisionBundle` row    | The action the pipeline produced at decision time. Immutable.                                                                                                                                                                                                      |
| `resolution_action` | `ResolutionAttempt` row | The action a resolver produced for a non-terminal decision. Immutable per row.                                                                                                                                                                                     |
| `realized_action`   | computed                | The action ultimately taken on the entity. For terminal `decision_action` (`ALLOW`, `BLOCK`) it equals `decision_action`; for non-terminal (`CHALLENGE`, `HOLD`) it is the first terminal `resolution_action` in the attempt chain, or `None` while still pending. |

### Resolution attempt log

For non-terminal `decision_action`, the realized outcome is recorded in the
append-only `decision_resolution_attempts` table as one or more
`ResolutionAttempt` rows per `decision_id`:

```python
class ResolutionAttempt(BaseModel):
    decision_id: str  # FK to DecisionBundle
    attempt_id: str  # UUID for this attempt
    sequence: int  # Ordering within decision_id
    started_at: datetime
    completed_at: datetime | None
    resolver_kind: ResolverKind  # HUMAN, SLA_DEFAULT, STEP_UP_AUTH, ...
    resolver_id: str
    status: ResolutionStatus  # PENDING | COMPLETED | ESCALATED | EXPIRED
    resolution_action: DecisionAction | None
    note: str
    evidence: dict[str, Any]  # Per-resolver-kind payload
```

Multi-step resolvers (escalation, outreach-initiated then confirmed) are
expressed as ordered attempts; new states are new rows, never row mutations. The
`realized_action` fold walks attempts in `sequence` order and returns the first
terminal `resolution_action`.

Replay does **not** re-invoke the gate. The `gate_response` and `gate_output`
are already in the bundle. Replay feeds the cached output back through
enforcement — the same deterministic rule evaluation that ran at decision time.
This is what "deterministic replay" means: verifying enforcement determinism,
not gate reproducibility.

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

| Action      | Meaning                                              |
|-------------|------------------------------------------------------|
| `ALLOW`     | Request proceeds without friction                    |
| `CHALLENGE` | MFA or step-up authentication required               |
| `HOLD`      | Request queued for human review; no immediate action |
| `BLOCK`     | Request denied; session terminated                   |

Action severity is ordered: `ALLOW < CHALLENGE < HOLD < BLOCK`. The enforcement
layer resolves to the most conservative permissible action when the policy gate
returns multiple permitted actions.
