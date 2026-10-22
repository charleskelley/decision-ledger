# Interfaces

The `core/` boundary, the framework's contracts, the seams through which gates
and identity resolvers plug in, the enforcement interface, and the ledger's
HTTP surface. Field-level detail for every class is in the API reference
([`core`](../reference/core.md), [`app`](../reference/app.md)); the published
wire contract is `schemas/submission-v2.json` and `schemas/bundle-v2.json`.

---

## The `core/` Boundary

`core/` is the one framework package with no infrastructure dependencies.
`app/`, `eval/`, and every workspace member import from it; it imports only
the standard library and Pydantic, nothing from `app/`, and never a reasoner
or a gate (DR-8, DR-11).

**What lives in `core/`:**

- Strict, frozen Pydantic models for every framework contract: the
  `Submission`, its `ReasonerContext` and `GateContext`, the
  `ReasonerRegistration`, the identity types, `GateRequest`, `GateOutput`,
  `EnforcementDecision`, `DecisionBundle`, resolution attempts, eval metrics.
- Enums (`DecisionAction`, `LabelType`, `ResolverKind`, `ResolutionStatus`,
  `EvalDimension`).
- Protocols: `ReasonerRegistry`, `IdentityResolver`, `LLMClient`.
- Pure functions: intake and bypass-rule checks, the predicate evaluator, the
  combinator, the entity-id derivation, the membership fold.
- Exception types, including `LedgerError`, which every API operation raises.

**What does not:** network I/O of any kind, database drivers, HTTP frameworks
or clients, LLM SDKs, ML libraries, and any reasoner's or gate's own types
(ATO's `LoginEvent`, the RAG policy gate's `PolicyGateVerdict`). The test: if
an import makes a `core/` test need `docker compose up`, it does not belong
there.

---

## Package Layout

```
core/                          # Framework contracts: no infrastructure, no domain types
├── actions.py                 # DecisionAction: ALLOW, CHALLENGE, HOLD, BLOCK
├── submission.py              # Submission, BypassCitation, BypassCheck; validate_submission,
│                              #   effective_gate_set, matching_bypass_rule, evaluate_bypass,
│                              #   evaluate_competing_rules, storage_problem
├── identity.py                # NativeSubject, NativeNamespace, ResolvedIdentity, IdentityProvenance,
│                              #   RelatedEntity, EntityDerivation, IdentityResolver, derive_entity_id,
│                              #   EntityEvent, JournalProvenance, Membership, project_membership,
│                              #   linked_entities (a leaf: imports nothing from core)
├── bundle.py                  # DecisionBundle (schema version 2), GateRecord
├── gate_request.py            # GateRequest
├── enforcement.py             # EnforcementDecision
├── exceptions.py              # Framework exceptions and LedgerError
├── observation/
│   ├── reasoner_context.py    # ReasonerContext, LabelType, AttributionSummary, Contribution
│   ├── gate_context.py        # GateContext
│   └── registration.py        # GateBinding, BypassRule, ReasonerRegistration, ReasonerRegistry,
│                              #   registration_version, canonical_registration
├── gate/
│   ├── input.py               # GateInput
│   ├── output.py              # GateOutput (open), EvidenceRef, EvidenceStatus
│   ├── verdict.py             # GateVerdict
│   ├── predicate.py           # Compare, AllOf, AnyOf, Not; evaluate_predicate, parse_predicate
│   └── combinator.py          # SEVERITY_ORDER, severity, most_restrictive, GateStatus
├── resolution/
│   ├── attempt.py             # ResolutionAttempt, ResolverKind, ResolutionStatus
│   ├── human.py               # HumanResolutionAttempt
│   └── sla_default.py         # SlaDefaultResolutionAttempt
├── llm/
│   └── client.py              # LLMClient protocol, CompletionResult, TokenUsage (DR-23)
└── eval/
    └── metrics.py             # EvalDimension, per-dimension metrics, DimensionResult, EvalReport
```

The `observation/` package name is historical; it holds what a reasoner
supplies and what the ledger knows about it. A reasoner's domain types stay
in its own member: for ATO, `ato_reasoner/events.py` (`LoginEvent`),
`features.py` (`AtoFeatureVector`), `scorer/output.py` (`ScorerOutput`), and
`assembler.py` (`build_submission`).

---

## The Submission

`core.submission.Submission` is the wire contract (DR-31): one strict, frozen,
JSON-serializable model that an in-process caller, an HTTP client, and an
agent all hand over. Unknown fields are rejected at every level, and every
value must survive the bundle's JSON round trip (no non-finite number, NUL
character, or invalid Unicode anywhere). Parse wire input with
`Submission.model_validate_json`.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | `Literal[2]` | Wire schema version |
| `reasoner_id` | `str` | The submitting reasoner; must equal `reasoner_context.reasoner_id` |
| `event_id` | `str` | The reasoner's event id; the default idempotency key |
| `timestamp` | aware `datetime` | When the event happened |
| `entity_id`, `entity_type` | `UUID`, `str` | The reasoner's own id for the subject |
| `native_subject` | `NativeSubject`, optional | The subject in the reasoner's own terms, which the ledger resolves (DR-32) |
| `mode` | `"record"` or `"gate"` | Record a decision made, or ask for a gate (DR-30) |
| `proposed_action` | `DecisionAction`, optional | Required on a record; an optional floor on a gate request |
| `bypass` | `BypassCitation`, optional | `rule_id` and optional `rule_version`; required on a record, refused on a gate request |
| `reasoner_context` | `ReasonerContext` | The reasoner's evidence |
| `gate_contexts` | `tuple[GateContext, ...]` | One per bound gate, on every submission (records too) |
| `raw_event` | `dict[str, JsonValue]` | The reasoner's event, opaque, stored verbatim |

**`ReasonerContext`** (`core/observation/reasoner_context.py`):
`reasoner_id`, `reasoner_name`, `model_version`, `model_artifact_sha256`,
`inference_latency_ms`, `label_type` (`NUMERICAL` or `CATEGORICAL`),
`label_name`, `label_value`, `feature_set` (the complete snapshot of scalar
features the model consumed; bypass rules, the rules gate, and Tier 3 read
it), and `attribution` (`AttributionSummary`: contributions, importance, a
narrative). Its docstring says what each field means for an LLM-agent
reasoner (model id, prompt digest, tool-trace digest).

**`GateContext`** (`core/observation/gate_context.py`): `gate_id` and
`gate_config` (JSON), validated at intake against the gate's `config_model`.

**`NativeSubject`** (`core/identity.py`, re-exported by `core.submission`):
`native_namespace` and `native_subject_id`.

**Helpers** shared by reasoners and the ledger, all pure:
`validate_submission` (the registration-dependent intake checks),
`effective_gate_set`, `matching_bypass_rule` (the strictest registered rule
that holds, for choosing the mode), `evaluate_bypass` and
`evaluate_competing_rules` (each returns `BypassCheck`: the rule, the values
its predicate read, `holds`, and any evaluation error).

---

## The Registration

`core.observation.ReasonerRegistration` is what the ledger knows about a
reasoner. It comes from an installed reasoner plugin or from the deployment's
registration file (`DECISIONLEDGER_REGISTRATIONS`), and is served read-only by
`GET /api/v1/reasoners`.

| Field | Meaning |
|-------|---------|
| `reasoner_id`, `reasoner_name` | Identity; `reasoner_id` is the tenant column |
| `gates` | `tuple[GateBinding, ...]`: `GateBinding(gate_id, mandatory=False)`, each gate once |
| `bypass_rules` | `tuple[BypassRule, ...]`: `BypassRule(rule_id, version, description, predicate, action)`, unique ids |
| `recordable_actions` | Actions the reasoner may record (default: all); every rule's action must be one |
| `native_namespaces` | `tuple[NativeNamespace, ...]`: per namespace, the `entity_type` it names and the UUID5 `namespace_uuid` its ids derive under |
| `novel_entity_feature` | Enforcement profile, Tier 3: the `feature_set` key whose truthy value marks a novel entity (`None`: Tier 3 never fires) |
| `jurisdiction_conflict_groups` | Enforcement profile, Tier 5: groups of mutually consistent jurisdictions (empty: no structural check) |

`registration_version(registration)` is the content address of the canonical
registration (16 hex digits of SHA-256). The ledger snapshots every version it
serves, every bundle records the version it was decided under, and replay
reads that snapshot.

**The predicate language** (`core/gate/predicate.py`), shared by bypass rules
and the rules gate, is a closed AST of four node kinds discriminated by
`kind`: `Compare(field, op, value)` with `op` one of `eq`, `ne`, `lt`, `le`,
`gt`, `ge`, `in`, `not_in`; `AllOf(predicates)` (`all`); `AnyOf(predicates)`
(`any`); and `Not(predicate)` (`not`). A field is `label_value` or
`feature_set.<name>`. `evaluate_predicate` is pure; a missing field or a
non-numeric ordering operand raises `PredicateError`, so the caller fails
closed. ATO's two rules, `allow-band` and `block-band`, are single `Compare`
nodes on `label_value`, registered in `ato_reasoner/registry.py`.

---

## Identity Types

`core/identity.py` holds the identity contract (DR-32). An
`IdentityResolver.resolve(native_namespace, native_subject_id)` returns a
`ResolvedIdentity` (`entity_id`, `entity_type`, `derivation`, `provenance`,
`related`) or `None` for no match. `IdentityProvenance` records the native
subject, `resolved_by`, `crosswalk_version`, `method` (`deterministic`,
`probabilistic`, or `unresolved`), `confidence`, `resolved_at`, and `reason`;
`RelatedEntity` is a flat `(relationship_type, entity_type, entity_id)`.
`derive_entity_id(namespace_uuid, enterprise_key)` is UUID5. The journal types
(`EntityEvent`, `JournalProvenance`, `Membership`) and the pure fold
`project_membership` define the identity journal and its projection. See
[Identity](identity.md#the-contract).

---

## The Gate Contracts

A gate reads a `GateRequest` and returns a `GateOutput` (DR-28).

**`GateRequest`** (`core/gate_request.py`): `decision_id`, `submission`, the
`gate_context` addressed to this gate, `budget_ms`, `identity` (the
resolved subject; `None` only when shadow evaluation re-runs a bundle written
before DR-32), and `binding_config` (the `config` of the reasoner's binding
to this gate, from the registration the decision runs under, DR-34). It lives at the top of `core/` because `core/gate/` may not
import `core/observation/`.

**`GateOutput`** (`core/gate/output.py`) is open: the framework types and
reads a universal base and stores the rest verbatim.

| Field | Meaning |
|-------|---------|
| `gate_id` | The gate that produced it (its verdict must name the same gate) |
| `deterministic` | A pure function of its request; enforcement skips Tiers 2 and 4 |
| `verdict` | A `GateVerdict`, or `None`: no valid verdict (Tier 1 holds) |
| `evidence` | `tuple[EvidenceRef, ...]`: `EvidenceRef(source_id, locator, jurisdictions, content_hash, artifact_hash)`; Tier 5 reads the union of jurisdictions; `content_hash` is the SHA-256 of the exact content relied on, `artifact_hash` the digest of the artifact the gate escrowed for it in the ledger's evidence escrow (DR-34) |
| `evidence_snapshot` | Opaque JSON, gate-owned, stored for replay and audit |
| `extra` | Opaque JSON object for gate-specific fields |

**`GateVerdict`** (`core/gate/verdict.py`): `gate_id`, `permitted_actions`,
`required_controls`, `confidence` (0 to 1), `escalate_to_human`,
`escalation_reason`. **`GateInput`** (`core/gate/input.py`) carries only
`gate_id`: the framework's record that it invoked a gate. Everything else a
gate consulted (a rendered prompt, retrieved passages, a rule-set version)
belongs in its own `evidence_snapshot` or `extra`. The framework never defines
or imports a subclass of any of these.

**`EvidenceStatus`** (`core/gate/output.py`, beside `EvidenceRef`): how one
evidence reference stands when its gate verifies it (DR-34) — `intact`,
`source_changed`, `source_unavailable`, or `not_verifiable`. A gate that
verifies its own references (`app.gates.EvidenceVerifier`) answers one per
reference; `POST /api/v1/decisions/{decision_id}/verify` reports them.

**The combinator** (`core/gate/combinator.py`): `SEVERITY_ORDER`
(ALLOW < CHALLENGE < HOLD < BLOCK), `severity`, and
`most_restrictive(proposed, enforced)`, a maximum over that order with `None`
as its identity. `GateStatus` is `ran`, `skipped`, or `failed`.

### The gate seam (`app/gates.py`)

| Type | Role |
|------|------|
| `GateKind` | `llm`, `lookup`, or `rules` |
| `GatePlugin` | What the `decisionledger.gates` entry point publishes: `gate_id` (equal to the entry-point name), `kind`, `deterministic`, `config_model` (a Pydantic model for `gate_config`, or `None`), `build(framework) -> Gate` |
| `Gate` | `async evaluate(request: GateRequest) -> GateOutput` (must not block the event loop) and `close()` |
| `ConfigAuthorizer` | Optional capability of a built gate: `authorize_config(config, *, reasoner_id)` raises `ValueError` to refuse a configuration for that reasoner (422 `GATE_CONFIG_UNAUTHORIZED`) |
| `BindingValidator` | Optional capability (DR-33): `validate_binding(reasoner_id, config)` raises `GateConfigError` to refuse a registration's binding (startup fails, or the write is 422 `GATE_BINDING_REFUSED`) |
| `ReasonerScope` | Optional capability: `serves(reasoner_id)`; startup refuses a registration that binds a gate that cannot serve it |
| `EvidenceVerifier` | Optional capability (DR-34): `async verify(output, *, live) -> tuple[EvidenceStatus, ...]`, one status per reference of an output the gate returned, against its own record or, live, its source |
| `ComposedGate` | A built gate with its plugin and whether the deployment mandates it |
| `invoke_gate(composed, request)` | Runs one gate under its budget and returns a `GateInvocation`; a gate that is not composed, raises, times out, or returns an unusable output fails closed with the reason |
| `verify_evidence(composed, output, *, live, budget_ms)` | Asks a gate how its references stand; a gate that is not composed, lacks `EvidenceVerifier`, raises, answers late, or answers out of contract leaves every reference `not_verifiable` |

To write a gate, see [How to implement a gate](gates/implementation.md) and
the [new-gate checklist](reasoners/reasoner-handoff.md#new-gate-checklist).

---

## The Decision Bundle

`core.bundle.DecisionBundle` (schema version 2) is the immutable record of one
decision. It stores the submission verbatim, so replay needs no reasoner code,
no gate, and no corpus.

| Field | Meaning |
|-------|---------|
| `schema_version`, `decision_id`, `created_at` | Identity of the record |
| `submission` | The reasoner's `Submission`, as received |
| `identity` | The `ResolvedIdentity` the ledger established (`None` only before DR-32); `entity_id` and `entity_type` are properties that return it |
| `idempotency_key`, `ingestion_timestamp` | The key (default: `event_id`) and when the ledger received the submission |
| `gates` | One `GateRecord` per gate in the gate set |
| `decision_action` | The final action |
| `decision_level_rule`, `override_log` | The decision-level rule that fired, if any, and the decision-level log |
| `bypass_rule_holds`, `bypass_check`, `other_bypass_checks` | A record's cited rule and every other registered rule, as evaluated (`None` and empty on a gated decision) |
| `registration_version` | The registration version the decision was made under |
| `latency_breakdown` | The ledger's step durations (`identity_ms`, `gate_ms`, `enforcement_ms`) |

**`GateRecord`**: `gate_id`, `mandatory`, `status`, `reason`, `input`,
`output`, `enforcement` (the gate-level `EnforcementDecision`), and
`duration_ms`. A `ran` record has an input, an output, and enforcement; a
`failed` record has no output, a reason, and its fail-closed enforcement; a
`skipped` record has only its reason.

**Resolution attempts** (`core/resolution/`, DR-18). A CHALLENGE or HOLD is
resolved by appending `ResolutionAttempt` rows (`decision_id`, `attempt_id`,
`sequence`, `started_at`, `completed_at`, `resolver_kind`, `resolver_id`,
`status`, `resolution_action`, `note`); `HumanResolutionAttempt` adds
`reviewer_role` and `reference_ticket_id`, and `SlaDefaultResolutionAttempt`
adds `entity_tier` and `sla_window_seconds`. The bundle never changes; the
realized action is derived from the attempt chain at read time.

---

## Enforcement Interface

`app/enforcement/resolver.py` is pure: no network calls, no I/O. That is what
makes deterministic replay possible.

```python
def resolve_gate_output(
    gate_output: GateOutput | None,
    *,
    registration: ReasonerRegistration,
    decision_id: str,
    failure: str | None = None,
) -> EnforcementDecision:
    """Gate-level Tiers 1, 2, 4, 5, and the default, for one gate."""

def resolve_decision(
    *,
    submission: Submission,
    records: Sequence[GateRecord],
    registration: ReasonerRegistration,
    bypass_check: BypassCheck | None,
    decision_id: str,
    competing_checks: Sequence[BypassCheck] = (),
) -> EnforcementDecision:
    """Decision-level rules (Tier 3, bypass floors) and the combinator."""

def credited_rule(bundle: DecisionBundle) -> str | None:
    """The one rule a single-rule summary credits (the receipt's enforcement_rule_applied)."""
```

`EnforcementDecision` (`core/enforcement.py`) is `decision_action`,
`enforcement_rule_applied`, and `override_log`; each `ran` or `failed` gate record carries one,
and the decision-level one becomes the bundle's `decision_action`,
`decision_level_rule`, and `override_log`. [Pipeline](pipeline.md#6-gate-level-enforcement)
gives the tiers and rules in full.

---

## External API Surface

The ledger serves only its own routes (`app/main.py`, `app/api/`); it mounts
no reasoner router (DR-31). Every `/api/v1` route authenticates the caller by
the `X-DecisionLedger-Key` header (`app/auth.py`): a **reasoner** key writes
and reads its own reasoner's decisions (another tenant's decision reads as
404), the **operator** key reads every decision and resolves held ones but
writes none, and `LocalClient` acts as the **trusted** in-process principal.
Keys come from `DECISIONLEDGER_API_KEYS` (`reasoner_id:key` pairs),
`DECISIONLEDGER_API_KEYS_FILE`, and `DECISIONLEDGER_OPERATOR_KEY`; with none
configured, every `/api/v1` route answers 401. The OpenAPI document is served
at `/openapi.json`.

| Route | Caller | Returns |
|-------|--------|---------|
| `GET /health` | none | `{"status": "ok"}` |
| `POST /api/v1/decisions` | reasoner | 201 `DecisionReceipt` and `Location`; body: one `Submission` (at most 1 MiB) |
| `POST /api/v1/decisions:batch` | reasoner | 200 `BatchResult`; body: NDJSON of recorded submissions (at most 64 MiB, 10,000 lines); each line recorded or rejected on its own |
| `GET /api/v1/decisions/{decision_id}` | reasoner, operator | The stored bundle, any schema version |
| `POST /api/v1/decisions/{decision_id}/replay` | reasoner, operator | `ReplayResponse`: original and replayed actions, per-gate results, `matched` |
| `POST /api/v1/decisions/{decision_id}/verify` | reasoner, operator | `EvidenceVerification`: per evidence reference of each gate that ran, `escrow` (`present`, `missing`, `none`) and the gate's `status` (`EvidenceStatus`), and a summary; query `live` (default `false`) also checks each source as it is now (DR-34) |
| `POST /api/v1/decisions/{decision_id}/resolutions` | operator | 201 `ResolutionReceipt`; body: `ResolutionRequest` (`resolver_id`, `resolution_action`, `note`, optional `reviewer_role`, `reference_ticket_id`) |
| `GET /api/v1/entities/{entity_id}/decisions` | reasoner, operator | `EntityDecisions`, newest first; query `reasoner_id`, `limit` (1 to 500, default 50), `before`, `before_id`, `include_related` |
| `GET /api/v1/identity/resolve` | any key | `ResolvedIdentity`; query `namespace`, `native_id` |
| `GET /api/v1/reasoners` | any key | `ReasonerList`: every served registration, its version and source |
| `GET /api/v1/reasoners/{reasoner_id}` | any key | One registration as served now |
| `GET /api/v1/reasoners/{reasoner_id}/registrations/{version}` | any key | One stored registration version |
| `GET /api/v1/gates` | any key | `GateList`: each composed gate's id, kind, determinism, mandate, and `gate_config` JSON schema |

The wire models are in `app/api/models.py`. `app.client.LedgerClient` is the
same surface in Python: `HttpClient` calls these routes, `LocalClient` calls
the same operations in process, and both return the same models and raise
the same `LedgerError`. Every write is synchronous; there is no idempotency on
the wire (a retried POST records twice). The timeline and
`include_related` are described in [Identity](identity.md#timelines).

---

## Error Types

### Problem documents

Every error the ledger returns is an RFC 9457 `application/problem+json`
document (`app/api/errors.py`) with `type`
(`urn:decisionledger:problem:<code>`, the code lowercased with hyphens),
`title`, `status`, `detail`, `instance`, a stable `code`, and extension
members (a validation `errors` list never echoes the input). FastAPI's own
validation errors, routing errors, and unhandled exceptions are rendered the
same way. Clients switch on `code`:

| Route | Status and codes |
|-------|------------------|
| Any `/api/v1` route | 401 `UNAUTHENTICATED` (no key, or an unknown key); 422 `REQUEST_INVALID` (a path or query parameter); 500 `INTERNAL_ERROR` |
| A path or method no route serves | The HTTP status name as the code (for example 404 `NOT_FOUND`, 405 `METHOD_NOT_ALLOWED`) |
| `POST /decisions` | 413 `BODY_TOO_LARGE`; 422 `SUBMISSION_INVALID`; 403 `REASONER_MISMATCH`, `REASONER_KEY_REQUIRED`; 422 `REASONER_NOT_REGISTERED`; 422 with a `SubmissionContractError` code: `BYPASS_RULE_UNREGISTERED`, `ACTION_NOT_RECORDABLE`, `BYPASS_ACTION_MISMATCH`, `BYPASS_RULE_VERSION_MISMATCH`, `NATIVE_SUBJECT_MISMATCH`, `GATE_NOT_BOUND`, `GATE_CONTEXT_MISSING`, `NO_GATES`, `GATE_CONFIG_INVALID`, `GATE_CONFIG_UNAUTHORIZED` |
| `POST /decisions:batch` | 413 `BODY_TOO_LARGE`, `BATCH_TOO_LARGE`; 422 `BATCH_EMPTY`; 403 `REASONER_MISMATCH`, `REASONER_KEY_REQUIRED` (a line for another reasoner refuses the whole batch). A rejected line carries its own code in the `BatchResult`: `SUBMISSION_INVALID`, `REASONER_NOT_REGISTERED`, a `SubmissionContractError` code, or `BATCH_RECORD_ONLY` for a gated line |
| `GET /decisions/{id}` | 404 `DECISION_NOT_FOUND`; 500 `BUNDLE_UNREADABLE` |
| `POST /decisions/{id}/replay` | 404 `DECISION_NOT_FOUND`; 409 `BUNDLE_SCHEMA_UNSUPPORTED`, `REASONER_NOT_COMPOSED`, `REGISTRATION_SNAPSHOT_MISSING`; 500 `BUNDLE_UNREADABLE` |
| `POST /decisions/{id}/verify` | 404 `DECISION_NOT_FOUND`; 409 `BUNDLE_SCHEMA_UNSUPPORTED`; 500 `BUNDLE_UNREADABLE` (a gate that cannot answer leaves its references `not_verifiable`; it is no error) |
| `POST /decisions/{id}/resolutions` | 403 `OPERATOR_REQUIRED`; 413 `BODY_TOO_LARGE`; 422 `RESOLUTION_INVALID`; 404 `DECISION_NOT_FOUND`; 409 `DECISION_NOT_RESOLVABLE` (an ALLOW or BLOCK decision), `DECISION_ALREADY_RESOLVED`, `RESOLUTION_CONFLICT`; 503 `RESOLUTIONS_UNAVAILABLE` |
| `GET /entities/{id}/decisions` | 422 `ENTITY_ID_INVALID`, `REQUEST_INVALID`; 403 `REASONER_MISMATCH` (a reasoner key asking for another reasoner's decisions) |
| `GET /identity/resolve` | 422 `REQUEST_INVALID`; 404 `IDENTITY_UNRESOLVED` (no resolver match and no registration declares the namespace) |
| `GET /reasoners/{id}`, `.../registrations/{version}` | 404 `REASONER_NOT_FOUND`, `REGISTRATION_VERSION_NOT_FOUND` |

### Exception classes

All framework exception types live in `core/exceptions.py`. Infrastructure
errors are not wrapped in them: a route answers an unhandled one (a psycopg
error, say) as 500 `INTERNAL_ERROR`, and `HttpClient` lets an `httpx`
transport error propagate.

| Exception | When raised | Surfaces as |
|-----------|-------------|-------------|
| `SubmissionContractError` | A submission breaks its registration or a gate's configuration (`validate_submission`, gate config checks); carries a stable `code` | 422 with that code |
| `UnregisteredReasonerError` | A submission (or a replay without a registration snapshot, or a registration lookup) names a reasoner the deployment does not register | 422 `REASONER_NOT_REGISTERED`; on replay 409 `REASONER_NOT_COMPOSED`; on `GET /reasoners/{id}` 404 `REASONER_NOT_FOUND` |
| `BundleSchemaUnsupportedError` | Typed loading or replay of a bundle that is not schema version 2 | 409 `BUNDLE_SCHEMA_UNSUPPORTED` |
| `RegistrationSnapshotMissingError` | Replay of a bundle whose registration version has no snapshot and is not current | 409 `REGISTRATION_SNAPSHOT_MISSING` |
| `PredicateError` | A predicate cannot be evaluated (missing field, non-numeric ordering operand) | The caller fails closed: the bypass rule does not hold, or the rules gate holds |
| `LedgerError` | Any API operation refusal: `status`, `code`, `detail` | The problem document; `HttpClient` raises it again from the document |
| `ReasonerPluginError` | A reasoner plugin or eval adapter cannot be discovered or validated | Startup (or eval harness) failure |
| `GatePluginError` | A gate plugin cannot be discovered, built, or trusted, or a mandated gate is not composed | Startup failure (a gate that misbehaves at decision time fails closed instead) |
| `ResolverPluginError` | The configured identity resolver cannot be composed | Startup failure (a resolver that misbehaves at decision time gives an unresolved subject instead) |
| `RegistrationError` | The registration file cannot be read or validated, a reasoner is registered twice, or two registrations declare one native namespace differently | Startup failure |

All errors are logged as structured JSON with `component` and, where known,
`event_id` and `decision_id`.
