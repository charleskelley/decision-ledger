# Data

The data shapes of one ATO decision, from the login event to the stored
Decision Bundle: the event and its deduplication, the feature vector, ATO's
policy corpus, the scorer output, the submission ATO sends, what the RAG
policy gate returns, the version-2 bundle the ledger writes, and how the
bundle is read, replayed, and resolved.

The event, features, scorer output, corpus, and every value shown in the
examples are ATO's. The submission, gate output, bundle, and resolution
shapes are the framework's (`core/`), shown here as an ATO decision fills
them.

---

## Login event

Every event entering the pipeline is a `LoginEvent`
(`ato_reasoner/events.py`). It is ATO's own domain type, not a framework
type: it reaches the ledger only as the opaque `raw_event` JSON of a
submission.

```python
class LoginEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, allow_inf_nan=False)

    # Identity the submission is built from
    event_id: str = Field(min_length=1)
    entity_type: Literal["account"] = "account"
    timestamp: AwareDatetime  # timezone-aware

    # Domain identity
    account_id: str  # ATO's business key; the native subject's key
    session_id: str

    # Network
    ip_address: str  # IPv4 or IPv6
    geolocation: Geolocation

    # Device
    device_fingerprint: str
    user_agent: str

    # Auth context
    auth_method: AuthMethod  # PASSWORD, MFA_TOTP, MFA_PUSH, SSO, PASSKEY, MAGIC_LINK
    outcome: AuthOutcome  # SUCCESS, FAILURE, TIMEOUT, BLOCKED

    # Generator only; cleared by the ingestion consumer before scoring
    scenario_tag: str | None = None

    @computed_field
    @property
    def entity_id(self) -> UUID: ...  # uuid5(ATO's account namespace, account_id)

    @property
    def native_subject(self) -> NativeSubject: ...  # ("ato-account", account_id)
```

```python
class Geolocation(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, allow_inf_nan=False)

    latitude: float
    longitude: float
    country: str  # ISO 3166-1 alpha-2
    city: str
    asn: str  # Autonomous System Number, e.g. "AS15169"
```

An event accepts nothing its submission would refuse: a naive timestamp, an
empty `event_id`, a non-finite coordinate, or a string the ledger cannot
store. A bad login is rejected when it is parsed (HTTP 422 from ATO's
service, or the dead-letter stream for the consumer), before its features
are written.

`entity_id` is derived from `account_id` under the UUID5 namespace ATO's
registration declares (`ATO_ACCOUNT_NAMESPACE`, namespace `ato-account`,
subject type `account`), so any caller can reproduce it from the business key
without a lookup.

### Deduplication

The ingestion consumer (`ato_reasoner/ingestion/`) reads the Redis stream
`ato:login-events`, validates each message as a `LoginEvent`, dead-letters
events older than the lateness window (default 1 hour), and deduplicates on
the event id: the Redis key is `ato:dedup:` plus the SHA-256 of `event_id`,
set with `SET NX EX` (default TTL 24 hours), so the check and the store are
one atomic step. Schema errors, late events, and duplicates go to
`ato:login-events:dlq` with a `reason` of `schema_error`, `lateness`, or
`duplicate`.

The bundle's own `idempotency_key` is the submission's `event_id` unless an
in-process caller supplies one.

---

## Feature vector

Computed by the online feature layer (`ato_reasoner/feature_service.py`) from
the event and the account's sliding-window state in Redis. It is an ATO
domain type (`ato_reasoner/features.py`), not a framework type.

```python
class AtoFeatureVector(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    entity_type: Literal["account"] = "account"
    computed_at: datetime
    account_id: str
    sparse_history: bool  # fewer than 10 historical events

    # Velocity features (event counts, >= 0)
    velocity_1min: int
    velocity_5min: int
    velocity_60min: int
    velocity_1440min: int  # 24-hour window

    # Novelty features, [0.0, 1.0]: 0 = seen before, 1 = never seen
    ip_novelty: float
    device_novelty: float
    geo_novelty: float  # country-level novelty for this account

    # Geographic signals
    impossible_travel: bool
    travel_speed_kmh: float | None  # None if no previous location on record

    # Device signals, [0.0, 1.0]
    device_consistency_score: float
    user_agent_consistency: float

    # Window metadata: which windows had enough history
    windows: list[WindowSpec]

    @computed_field
    @property
    def entity_id(self) -> UUID: ...  # same derivation as LoginEvent
```

The assembler flattens the twelve model features (velocities, novelties,
`impossible_travel`, `travel_speed_kmh`, the two consistency scores, and
`sparse_history`) into `ReasonerContext.feature_set`, so the submission
carries every feature the scorer consumed. A missing `travel_speed_kmh` is
recorded as `"N/A"`. `sparse_history` is the feature ATO's registration names
for the novel-entity tier (Tier 3).

---

## Policy corpus

ATO's policy corpus is 32 Markdown documents with YAML front matter under
`ato_reasoner/corpus/` (`guidance/`, `internal/`, `regulations/`,
`standards/`), the directory ATO's plugin exposes as `corpus_dir`. The RAG
policy gate owns the format, the chunking, and the stores: its corpus loader
parses the front matter into the gate's `PolicyDocument`
(`policy_rag_gate.models.corpus`), splits each body at `##` boundaries (and at `###`
for a section over 300 words, merging sections under 30 words), and indexes
the chunks under `reasoner_id=ato-reasoner` in Elasticsearch and in its
`policy_rag.policy_chunks` table (`make build-policy-index-ato-reasoner`). See
[Policy Corpus](../../gates/policy/policy-corpus.md) for the format.

```python
class PolicyDocument(BaseModel):  # policy_rag_gate.models.corpus; front matter of every document
    model_config = ConfigDict(strict=True, frozen=True)

    policy_id: str  # e.g. "NIST-SP-800-63B", "INT-AUTH-RISK-V2"
    title: str
    version: str  # e.g. "2.1", "4.0"
    jurisdiction: str  # ATO's values: US_FEDERAL, US_STATE, EU_GDPR, INTERNAL
    effective_date: date
    supersedes: str | None  # policy_id of the document this version replaces
    risk_tier: str | None  # ATO's values: STANDARD, HIGH_VALUE, ENTERPRISE; None = all tiers
    document_type: DocumentType  # REGULATION, GUIDANCE, INTERNAL_POLICY, STANDARD
```

The jurisdiction and risk-tier vocabularies are ATO's (`ato_reasoner/policy.py`);
the gate stores them as plain strings.

**Public sources (synthetic adaptations).** The regulation, guidance, and
standard documents are adaptations of public texts focused on
authentication risk, each marked as such: for example NIST SP 800-63B and
SP 800-53 (access control), the FFIEC authentication guidance and IT
handbook, GLBA and FTC Safeguards, NYDFS 23 NYCRR 500, GDPR Article 32,
ENISA authentication guidelines, PCI DSS v4.0 Requirement 8, ISO/IEC 27001
Annex A.9, SOC 2 CC6, and FIDO2/WebAuthn.

**Synthetic internal documents (deliberate complexity).**

- Superseded versions: `INT-AUTH-RISK-V1` (1.0) and `INT-AUTH-RISK-V2`
  (2.1) carry conflicting risk-score thresholds, and `INT-ATO-DETECT-V1` and
  `-V2` are a second superseded pair; each V2 names its predecessor in `supersedes`, so retrieval
  must prefer the current version.
- Jurisdiction variants: `INT-DATA-MIN-GDPR-V1` (`EU_GDPR`) states EU data
  minimization obligations that conflict with US retention requirements
  (`GLBA-SAFEGUARDS`); a retrieval set holding both is what ATO's Tier 5
  conflict groups flag.
- Tier-specific overrides: `INT-HVA-POLICY-V2` (`risk_tier: HIGH_VALUE`) and
  `INT-ENT-AUTH-V1` (`ENTERPRISE`) apply when the gate context's `risk_tier`
  matches.

---

## Scorer output

`ScorerOutput` is ATO's internal type (`ato_reasoner/scorer/output.py`). It
never appears in a submission or a bundle, and it does not decide the mode.

```python
class ScorerOutput(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    entity_id: UUID
    risk_score: float  # [0.0, 1.0]
    top_signals: list[Contribution]  # top 5 by absolute SHAP value
    scorer_version: str  # the model card's model_version, e.g. "1.0.1"
    scorer_artifact_sha256: str | None = None
    inference_latency_ms: float
```

`Contribution` is the framework's attribution type
(`core.observation`: `feature_name`, `feature_value`, `method`, `value`;
ATO's `method` is `shap`). The assembler translates the output into the
`ReasonerContext`:

| `ReasonerContext` field | From |
|---|---|
| `label_type`, `label_name`, `label_value` | `NUMERICAL`, `risk_score`, `scorer_output.risk_score` |
| `feature_set` | the flattened `AtoFeatureVector` |
| `attribution.feature_contributions` | `scorer_output.top_signals` |
| `model_version`, `model_artifact_sha256`, `inference_latency_ms` | the scorer's version, artifact digest, and latency |

---

## Submission

`build_submission` (`ato_reasoner/assembler.py`) returns one
`core.submission.Submission` (schema version 2), the only thing ATO hands the
ledger. A login scored 0.04 is recorded under `allow-band` (abridged; values
illustrative):

```json
{
  "schema_version": 2,
  "reasoner_id": "ato-reasoner",
  "event_id": "evt-0001",
  "timestamp": "2026-10-16T14:02:11Z",
  "entity_id": "ded8f151-4bc1-5c57-b4b6-6a676b959cb9",
  "entity_type": "account",
  "native_subject": {"native_namespace": "ato-account", "native_subject_id": "acct-48213"},
  "mode": "record",
  "proposed_action": "ALLOW",
  "bypass": {"rule_id": "allow-band", "rule_version": "1"},
  "reasoner_context": {
    "reasoner_id": "ato-reasoner",
    "reasoner_name": "ATO Reasoner",
    "model_version": "1.0.1",
    "label_type": "NUMERICAL",
    "label_name": "risk_score",
    "label_value": 0.04,
    "feature_set": {"velocity_1min": 1, "ip_novelty": 0.0, "travel_speed_kmh": "N/A", "sparse_history": false, "...": "..."},
    "attribution": {"feature_contributions": [{"feature_name": "device_consistency_score", "feature_value": 0.93, "method": "shap", "value": -1.2}]}
  },
  "gate_contexts": [
    {
      "gate_id": "policy",
      "gate_config": {
        "template_id": "ato-v3",
        "template_vars": {"risk_score": "0.040", "risk_tier": "STANDARD", "auth_method": "PASSWORD", "...": "..."},
        "jurisdictions": ["INTERNAL", "US_FEDERAL"],
        "risk_tier": "STANDARD",
        "retrieval_query": "device fingerprint consistency mismatch anomaly password ...",
        "corpus_version": "ato-v1"
      }
    }
  ],
  "raw_event": {"event_id": "evt-0001", "account_id": "acct-48213", "geolocation": {"...": "..."}, "...": "..."}
}
```

A login scored between the bands has `"mode": "gate"`,
`"proposed_action": null`, and `"bypass": null`; everything else has the same
shape. The policy gate's context travels on recorded submissions too, for
shadow evaluation. The JSON Schema is `schemas/submission-v2.json`; the
fields are described in the [Submission contract](../reasoner-handoff.md).

---

## Gate output

When ATO asks for a gate, the ledger runs the RAG policy gate (`gate_id`
`policy`). Every gate returns the framework's universal, open `GateOutput`
(`core/gate/output.py`, DR-28); the framework stores it and enforces its
verdict, and never imports a gate's own types.

```python
# core/gate/verdict.py: what enforcement reads
class GateVerdict(BaseModel):
    gate_id: str
    permitted_actions: list[DecisionAction]
    required_controls: list[str]  # sentinels such as "adversarial_probe_detected", "jurisdiction_conflict"
    confidence: float  # [0.0, 1.0]
    escalate_to_human: bool
    escalation_reason: str | None


# core/gate/output.py
class EvidenceRef(BaseModel):
    source_id: str  # the policy gate: the snippet's document id
    locator: str | None = None  # the policy gate: the snippet's section path
    jurisdictions: frozenset[str] = frozenset()  # Tier 5 reads their union


class GateOutput(BaseModel):
    gate_id: str
    deterministic: bool  # False for the policy gate: Tiers 2 and 4 apply
    verdict: GateVerdict | None  # None: no valid verdict, Tier 1 holds the decision
    evidence: tuple[EvidenceRef, ...] = ()
    evidence_snapshot: JsonValue | None = None  # opaque, gate-owned, stored verbatim
    extra: dict[str, JsonValue] = {}  # opaque, gate-owned


# core/gate/input.py: the framework's record that it invoked a gate
class GateInput(BaseModel):
    gate_id: str
```

The policy gate fills the opaque fields with its own models
(`policy_rag_gate.models`), serialized as JSON; readers that know the gate
(its eval, ATO's baseline capture) re-validate them:

```python
class PolicyGateSnapshot(BaseModel):  # GateOutput.evidence_snapshot
    retrieval_query: str  # from gate_config["retrieval_query"]
    retrieval_results: list[RetrievedSnippet]  # in rank order
    retrieval_path: str  # "reranked", "rrf_only", or "skipped"
    index_version: str
    model_version: str  # the LLM model id
    prompt_template_id: str  # "ato-v3"
    prompt_template_version: str
    corpus_version: str  # from gate_config["corpus_version"]
    rendered_prompt: str
    prompt_snapshot: PromptSnapshot  # the template text verbatim
    template_vars: dict[str, str]


class PolicyGateExtra(BaseModel):  # GateOutput.extra
    rationale: str | None = None
    citations: list[Citation] = []  # policy_id, snippet, relevance
    response_text: str | None = None
    token_cost: TokenCost | None = None
    retrieval_ms: float = 0.0
    failure: Literal["missing_retrieval_query", "render_error", "llm_error"] | None = None
```

The gate asks the LLM for a `PolicyGateVerdict` (the universal verdict plus
`rationale` and `citations`), converts it to the universal `GateVerdict`, and
records the rationale and citations in `extra`. Its evidence is one
`EvidenceRef` per retrieved snippet, carrying that snippet's jurisdiction.
See [Policy Gate](../../gates/policy/index.md).

---

## Decision bundle

The complete audit record of every decision (`core/bundle.py`, schema
version 2), stored in PostgreSQL (`decisionledger.decision_bundles`) and
indexed by `decision_id`. Every field is logged at decision time and the row
is never updated. The bundle stores ATO's submission verbatim and holds
nothing ATO-specific outside it.

```python
class DecisionBundle(BaseModel):
    schema_version: Literal[2] = 2
    decision_id: str
    created_at: datetime

    # Input: the reasoner's submission, verbatim (evidence, gate contexts, raw_event)
    submission: Submission

    # Subject: the identity the ledger resolved before any gate ran (DR-32)
    identity: ResolvedIdentity | None = None
    idempotency_key: str
    ingestion_timestamp: datetime  # when the ledger received the submission

    # Gates: one record per gate in the decision's gate set (DR-29)
    gates: tuple[GateRecord, ...]

    # Decision
    decision_action: DecisionAction
    decision_level_rule: str | None  # tier3_novel_entity, bypass_rule_not_satisfied, bypass_rule_superseded
    override_log: tuple[str, ...]  # the decision-level trail, ending with the combinator line

    # Record mode only (DR-30)
    bypass_rule_holds: bool | None
    bypass_check: BypassCheck | None  # the cited rule as evaluated, and the values it read
    other_bypass_checks: tuple[BypassCheck, ...] = ()  # every other registered rule

    registration_version: str | None = None  # the registration the decision was made under
    latency_breakdown: dict[str, float]  # identity_ms, gate_ms, enforcement_ms


class GateRecord(BaseModel):
    gate_id: str
    mandatory: bool
    status: Literal["ran", "skipped", "failed"]
    reason: str | None = None  # why it was skipped or failed
    input: GateInput | None = None
    output: GateOutput | None = None
    enforcement: EnforcementDecision | None = None  # the gate-level tiers on this output
    duration_ms: float | None = None
```

`bundle.entity_id` and `entity_type` are the resolved subject (model
properties read from `identity`; the JSON carries them under `identity`),
which is also the row's `entity_id` column and the timeline key; the
submission keeps ATO's own `entity_id`.

The login recorded above produces this bundle (as returned by
`GET /api/v1/decisions/{decision_id}`, the submission elided; ids and times
illustrative):

```json
{
  "schema_version": 2,
  "decision_id": "3f6c1a52-...",
  "created_at": "2026-10-16T14:02:12Z",
  "submission": {"...": "the submission above"},
  "identity": {
    "entity_id": "ded8f151-4bc1-5c57-b4b6-6a676b959cb9",
    "entity_type": "account",
    "derivation": {"namespace_uuid": "8a1f9b2e-4c3d-5e6f-7a8b-9c0d1e2f3a4b", "enterprise_key": "acct-48213"},
    "provenance": {
      "native_namespace": "ato-account",
      "native_subject_id": "acct-48213",
      "resolved_by": "passthrough",
      "crosswalk_version": null,
      "method": "unresolved",
      "confidence": null,
      "resolved_at": "2026-10-16T14:02:12Z",
      "reason": "no identity resolver is active (passthrough)"
    },
    "related": []
  },
  "idempotency_key": "evt-0001",
  "ingestion_timestamp": "2026-10-16T14:02:12Z",
  "gates": [
    {
      "gate_id": "policy",
      "mandatory": false,
      "status": "skipped",
      "reason": "mode=record: a recorded decision runs its mandatory gates only",
      "input": null,
      "output": null,
      "enforcement": null,
      "duration_ms": null
    }
  ],
  "decision_action": "ALLOW",
  "decision_level_rule": null,
  "override_log": [
    "bypass_rule: allow-band@1 holds — observed label_value=0.04",
    "bypass_rule_superseded: not fired — no other registered rule that holds justifies a stricter action",
    "tier3_novel_entity: skipped — recorded decision (the bypass rule is its justification)",
    "combinator: most_restrictive(proposed=ALLOW) → ALLOW"
  ],
  "bypass_rule_holds": true,
  "bypass_check": {
    "rule": {
      "rule_id": "allow-band",
      "version": "1",
      "description": "risk_score below 0.2: the scorer is confident the login is benign; record ALLOW without the policy gate",
      "predicate": {"kind": "compare", "field": "label_value", "op": "lt", "value": 0.2},
      "action": "ALLOW"
    },
    "observed": {"label_value": 0.04},
    "holds": true,
    "error": null
  },
  "other_bypass_checks": [
    {"rule": {"rule_id": "block-band", "...": "..."}, "observed": {"label_value": 0.04}, "holds": false, "error": null}
  ],
  "registration_version": "c71a46e19b322751",
  "latency_breakdown": {"identity_ms": 0.0, "gate_ms": 0.1, "enforcement_ms": 0.1}
}
```

On a gated login the policy gate's record has `"status": "ran"`, its
`input` (`{"gate_id": "policy"}`), the `output` described above, and its
gate-level `enforcement` (the action, the tier credited, and the tier log);
`bypass_rule_holds` and `bypass_check` are `null`, and the decision-level
log carries the Tier 3 line before the combinator line
(`combinator: most_restrictive(advisory=...)`). A policy gate that fails is
recorded `failed` with its reason and a Tier 1 HOLD. ATO's own step timings
(`features_ms`, `scorer_ms`, `ledger_ms`) are returned to ATO with the
receipt, not stored in the bundle. The JSON Schema is
`schemas/bundle-v2.json`.

### Replay fields

Replay needs nothing outside the bundle and the registration snapshot it
names:

| Replay requirement | Location in the bundle |
|---|---|
| Full feature snapshot | `submission.reasoner_context.feature_set` |
| Score and model version | `submission.reasoner_context.label_value`, `.model_version`, `.model_artifact_sha256` |
| Mode and cited rule | `submission.mode`, `submission.bypass` |
| Bypass rules as evaluated | `bypass_check`, `other_bypass_checks` (rule snapshots and observed values) |
| Gate contexts as sent | `submission.gate_contexts` |
| Policy gate retrieval and exact prompt | `gates[].output.evidence_snapshot` (`PolicyGateSnapshot`) |
| Cached gate verdicts | `gates[].output.verdict`, with `deterministic` and `evidence` |
| Enforcement profile (Tier 3 feature, Tier 5 groups) | the snapshot of `registration_version` |

### Replay contract

Given a bundle and the registration snapshot it records, replay re-runs the
gate-level tiers on each stored gate output, re-evaluates the stored bypass
rules against the stored submission's evidence, and re-runs the
decision-level rules and the combinator; the result must equal
`decision_action`. Replay does **not** re-invoke the gate or the LLM and
reads no corpus. The cached outputs go back through the same deterministic
enforcement that ran at decision time. That is what deterministic replay
means: verifying enforcement determinism, not gate reproducibility.

The ledger serves lookup and replay for any reasoner's bundle (DR-16, DR-31).
Every `/api/v1` route needs a key in `X-DecisionLedger-Key`: ATO's own key
reads ATO's decisions, and the operator key reads every reasoner's.

```bash
# Fetch a stored bundle
curl -s -H "X-DecisionLedger-Key: $ATO_LEDGER_KEY" \
  http://localhost:8000/api/v1/decisions/<decision_id>

# Replay it; "matched" must be true
curl -s -X POST -H "X-DecisionLedger-Key: $ATO_LEDGER_KEY" \
  http://localhost:8000/api/v1/decisions/<decision_id>/replay

# An account's decisions, newest first
curl -s -H "X-DecisionLedger-Key: $ATO_LEDGER_KEY" \
  "http://localhost:8000/api/v1/entities/<entity_id>/decisions?limit=50"
```

The replay response (`ReplayResponse`, `app/api/models.py`) carries
`decision_id`, `original_action`, `replayed_action`, `actions_match`,
`original_bypass_rule_holds`, `bypass_rule_holds`, one entry per gate
(`gate_id`, `status`, `original_action`, `replayed_action`), the
`registration_version` replay enforced against, and `matched`: the decision
action, every gate's enforced action, and (for a record) the bypass checks
all reproduced. A bundle of an older schema version reads through lookup and
answers replay with `409 BUNDLE_SCHEMA_UNSUPPORTED`. Replay is also covered
by the framework's integration suite (`tests/integration/test_bundle_store.py`,
`tests/integration/test_ledger_api.py`).

### Action vocabulary

The framework distinguishes three action concepts (DR-18):

| Term | Source | Meaning |
|---|---|---|
| `decision_action` | `DecisionBundle` row | The action the ledger produced at decision time. Immutable. |
| `resolution_action` | `ResolutionAttempt` row | The action a resolver produced for a non-terminal decision. Immutable per row. |
| `realized_action` | computed | The action ultimately taken on the subject. For a terminal `decision_action` (`ALLOW`, `BLOCK`) it equals `decision_action`; for a non-terminal one (`CHALLENGE`, `HOLD`) it is the first terminal `resolution_action` in the attempt chain, or `None` while pending. |

### Resolution attempt log

For a non-terminal `decision_action`, the realized outcome is recorded in the
append-only `decisionledger.decision_resolution_attempts` table as one or
more `ResolutionAttempt` rows per `decision_id` (`core/resolution/`):

```python
class ResolutionAttempt(BaseModel):
    decision_id: str  # the bundle's decision_id
    attempt_id: str  # UUID of this attempt
    sequence: int  # zero-based position within decision_id
    started_at: datetime
    completed_at: datetime | None
    resolver_kind: ResolverKind  # HUMAN, SLA_DEFAULT, STEP_UP_AUTH, ...
    resolver_id: str
    status: ResolutionStatus  # PENDING, COMPLETED, ESCALATED, EXPIRED
    resolution_action: DecisionAction | None
    note: str
```

Each resolver kind adds its own typed fields in a subclass
(`HumanResolutionAttempt`: `reviewer_role`, `reference_ticket_id`;
`SlaDefaultResolutionAttempt`: `entity_tier`, `sla_window_seconds`).
Multi-step resolutions (escalation, outreach initiated then confirmed) are
ordered attempts; a new state is a new row, never a mutation. The
`realized_action` fold walks attempts in `sequence` order and returns the
first terminal `resolution_action`.

An analyst resolves a held or challenged ATO decision with the operator key
(`DECISIONLEDGER_OPERATOR_KEY`); the resolution must be terminal:

```bash
curl -s -X POST \
  -H "X-DecisionLedger-Key: $DECISIONLEDGER_OPERATOR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"resolver_id": "analyst-7", "resolution_action": "ALLOW", "note": "Customer confirmed the new device.", "reviewer_role": "analyst"}' \
  http://localhost:8000/api/v1/decisions/<decision_id>/resolutions
```

The route appends a `COMPLETED` `HUMAN` attempt and answers `201` with a
`ResolutionReceipt` (`decision_id`, `attempt_id`, `sequence`,
`resolver_kind`, `resolver_id`, `status`, `resolution_action`,
`realized_action`, `completed_at`). A reasoner key gets
`403 OPERATOR_REQUIRED`; an ALLOW or BLOCK decision is
`409 DECISION_NOT_RESOLVABLE`, and one already resolved
`409 DECISION_ALREADY_RESOLVED`. The bundle is never touched.

---

## Action space

```python
class DecisionAction(StrEnum):  # core/actions.py
    ALLOW = "ALLOW"
    CHALLENGE = "CHALLENGE"
    HOLD = "HOLD"
    BLOCK = "BLOCK"
```

What each action means for a login (ATO's reading):

| Action | Meaning |
|---|---|
| `ALLOW` | The login proceeds without friction |
| `CHALLENGE` | MFA or step-up authentication required |
| `HOLD` | Queued for human review; no immediate action |
| `BLOCK` | Denied; session terminated |

Severity is ordered `ALLOW < CHALLENGE < HOLD < BLOCK`. Within one gate,
enforcement takes the most restrictive of the verdict's permitted actions;
across the proposal and the gates, the combinator takes the most restrictive
action.
