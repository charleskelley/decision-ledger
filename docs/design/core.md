# Interface

The `core/` module boundary, public contracts between pipeline components, and the external API surface.

---

## The `core/` Boundary

`core/` is the only module in the repository with no infrastructure dependencies. Everything else — `app/`, `eval/`, and every reasoner under `reasoners/` — imports from `core/`. `core/` imports from nothing internal, and never from a reasoner package.

**What lives in `core/`:**
- Pydantic models for every framework contract (`ReasonerContext`, `GateContext`, `DecisionBundle`, `ReasonerRegistration`, etc.)
- Enums and constants (`DecisionAction`, `GateRoute`, `DocumentType`)
- Protocol definitions for pluggable reasoners and gates (`Observation`, `ReasonerRegistry`, `PromptRegistry`, `LLMClient`)
- Exception types (`ObservationContractError`, `UnregisteredReasonerError`, `ReasonerPluginError`)
- Pure functions with no side effects (action severity ordering, idempotency key computation)
- Evaluation metric interfaces and dimension contracts

**What does NOT live in `core/`:**
- Redis clients or any network I/O
- SQLAlchemy, asyncpg, psycopg, or any database driver
- OpenAI SDK, anthropic SDK, or any LLM client
- FastAPI, Starlette, or any HTTP framework
- sentence-transformers, XGBoost, or any ML library
- Any reasoner's domain types (`LoginEvent`, `AtoFeatureVector`, ATO jurisdiction tags) — those live in the reasoner's own package

The test: if adding an import to `core/` requires `docker compose up` to pass, it does not belong there.

---

## Package Layout

```
core/                         # Framework contracts — no infrastructure, no domain types
├── actions.py                # DecisionAction enum, severity ordering
├── routes.py                 # GateRoute (FAST_PATH_ALLOW | FAST_PATH_BLOCK | ROUTE_TO_GATE)
├── bundle.py                 # DecisionBundle
├── snippet.py                # RetrievedSnippet (universal retrieved corpus chunk)
├── enforcement.py            # EnforcementDecision (enforcement-layer output)
├── exceptions.py             # ObservationContractError, UnregisteredReasonerError, ReasonerPluginError
├── observation/
│   ├── observation.py        # Observation protocol, validate_observation
│   ├── reasoner_context.py   # ReasonerContext, LabelType, AttributionSummary, Contribution
│   ├── gate_context.py       # GateContext
│   └── registration.py       # ReasonerRegistration (incl. enforcement profile), ReasonerRegistry
├── gate/
│   ├── input.py              # GateInput (universal base)
│   ├── output.py             # GateOutput (universal base)
│   ├── verdict.py            # GateVerdict (universal base)
│   └── policy/               # LLM-backed policy gate concrete contracts
│       ├── input.py          # PolicyGateInput(GateInput)
│       ├── output.py         # PolicyGateOutput(GateOutput), TokenCost
│       ├── verdict.py        # PolicyGateVerdict(GateVerdict)
│       ├── citation.py       # Citation
│       ├── corpus.py         # PolicyDocument, DocumentType
│       └── prompt.py         # PromptTemplate, PromptSnapshot (incl. system_text), PromptRegistry
├── resolution/
│   ├── attempt.py            # ResolutionAttempt, ResolverKind, ResolutionStatus
│   ├── human.py              # HumanResolutionAttempt
│   └── sla_default.py        # SlaDefaultResolutionAttempt (entity_tier, sla_window_seconds)
├── llm/
│   └── client.py             # LLMClient protocol, CompletionResult, TokenUsage (DR-23)
└── eval/
    └── metrics.py            # EvalDimension, per-dimension metrics, DimensionResult, EvalReport
```

Domain types are not in `core/`. Each reasoner keeps its own in its workspace member; for ATO that is `ato_reasoner` (`reasoners/account_takeover/src/ato_reasoner/`): `events.py` (`LoginEvent`, `Geolocation`, `AuthMethod`, `AuthOutcome`), `features.py` (`AtoFeatureVector`), `scorer/output.py` (`ScorerOutput`), `policy.py` (`Jurisdiction`, `RiskTier`), and `assembler.py` (`build_observation()`, the domain → framework boundary).

---

## Reasoning Layer Protocol

The gate is pluggable. Any reasoning layer that produces typed
`GateInput` / `GateOutput` subclasses (with a valid `GateVerdict`
subclass inside `GateOutput.verdict`) can replace the LLM policy gate.
The enforcement and audit layers depend only on the universal
`GateVerdict` base — they do not know or care what produced it.

Per DR-20, each gate kind subclasses the universal contracts in its own
subpackage (`core/gate/<kind>/`). See [`gates.md`](gates/implementation.md) for the
end-to-end implementation guide.

```python
from typing import Protocol
from core.snippet import RetrievedSnippet
from core.observation import Observation

class GateProtocol(Protocol):
    """Contract for any gate implementation.

    Any implementation — LLM, rules engine, ML classifier, human reviewer —
    produces typed input/output subclasses (e.g., PolicyGateInput,
    PolicyGateOutput). The framework reads GateOutput.verdict to drive
    enforcement; it does not interpret kind-specific fields.

    See DR-19 / DR-20 for the contract layering.
    """

    def evaluate(
        self,
        obs: Observation,
        snippets: list[RetrievedSnippet],
        *,
        decision_id: str,
        # Per-implementation kwargs as needed (e.g., corpus_version for
        # retrieval-using gates) — not part of the framework contract.
    ) -> GateResult:
        """Produce a GateResult carrying typed gate_input / gate_output.

        gate_output.verdict is None when the gate's response failed
        validation; in that case enforcement routes to HOLD via the
        schema-failure tier.
        """
        ...
```

### Implementing a new gate

See [`gates.md`](gates/implementation.md) for the full guide. In brief: subclass the
universal `GateInput`, `GateOutput`, `GateVerdict` in
`core/gate/<your-kind>/`. Narrow `gate_id` to a Pydantic
`Literal[<your-kind>]`. Add typed fields for kind-specific artifacts —
no `dict[str, Any]` escape hatches. Place the runtime orchestrator in
`app/gate/<your-kind>/`. Add the variant to the discriminated-union
deserializer in `app/audit/store.py`.

---

## Enforcement Interface

The enforcement layer takes a `GateVerdict` and produces a final
`EnforcementDecision`. It is a pure function — no network calls, no I/O.
This is what makes deterministic replay possible.

```python
from core.enforcement import EnforcementDecision
from core.observation import ReasonerRegistration
from core.gate import GateVerdict

def resolve(
    obs: Observation,
    gate_output: GateVerdict | None,  # bundle.gate_output.verdict at replay time
    *,
    snippets: list[RetrievedSnippet],
    decision_id: str,
    registration: ReasonerRegistration,
) -> EnforcementDecision:
    """Apply deterministic enforcement rules to produce the final action.

    Args:
        obs: Assembled Observation from the domain reasoner.
        gate_output: Validated GateVerdict, or None on schema-failure /
            fast path.
        snippets: Policy snippets used by the gate (for tier-5 jurisdiction
            conflict detection).
        decision_id: Decision UUID for log context.
        registration: The submitting reasoner's registration. Supplies the
            enforcement profile: novel_entity_feature (Tier 3) and
            jurisdiction_conflict_groups (Tier 5). Required, so a caller
            cannot silently disable either tier.

    Returns:
        EnforcementDecision with decision_action, enforcement_rule_applied,
        and override_log.

    Note:
        This function is pure and deterministic. Given the same inputs,
        it always produces the same output. This is the replay guarantee.
    """
    ...
```

---

## External API Surface (FastAPI)

The external API is minimal for MVP — it is a thin layer over the pipeline, not a product surface. `app/main.py:create_app()` is an application factory (`uv run uvicorn --factory app.main:create_app`): it serves the framework routes below and mounts the router of every reasoner plugin it discovers. Decision endpoints belong to reasoners, because only the reasoner knows its event type; lookup and replay belong to the framework, because they key on `decision_id` alone.

### Decision Endpoint (reasoner router)

ATO's router (`ato_reasoner/api.py`):

```
POST /api/v1/ato/decisions

Request body: LoginEvent (JSON; validated strictly in JSON mode — 422 on failure)
Response: AtoDecisionResponse (summary DTO, DR-16)
    - decision_id, decision_action, enforcement_rule_applied, route, latency_ms
```

### Audit Endpoints (framework router)

```
GET /api/v1/decisions/{decision_id}
    Returns: Full DecisionBundle (any reasoner)

POST /api/v1/decisions/{decision_id}/replay
    Returns: ReplayResponse (decision_id, original_action, replayed_action, actions_match)
```

Both return the same error envelope (`error_code`, `message`, optional `details`) under FastAPI's `detail` key:

| Status | `error_code` | When |
|--------|--------------|------|
| 404 | `DECISION_NOT_FOUND` | No bundle has this id, including an id that is not a well-formed UUID |
| 409 | `REASONER_NOT_COMPOSED` | The bundle exists, but its `reasoner_id` is not composed in this deployment (not installed, or excluded by `DECISIONLEDGER_REASONERS`); `details.reasoner_id` names it |

A resolution-history endpoint (`GET /api/v1/decisions/{decision_id}/resolution`) is designed (DR-18) but not built.

### Health

```
GET /health
    Returns: {"status": "ok"}
```

---

## Error Types

All exception types the framework contracts define live in `core/exceptions.py` and are imported by `app/`. Infrastructure-specific exceptions (e.g., a psycopg or Elasticsearch error) are caught at the `app/` boundary.

| Exception | Module | When raised |
|-----------|--------|-------------|
| `ObservationContractError` | `core.exceptions` | An Observation violates the intake contract (`validate_observation`): missing context, disallowed gate, template, or jurisdiction |
| `UnregisteredReasonerError` | `core.exceptions` | A `reasoner_id` is not in the registry: at intake, or when a stored bundle's reasoner is not composed (lookup/replay answer 409) |
| `ReasonerPluginError` | `core.exceptions` | Plugin or eval-adapter discovery fails: load error, protocol mismatch, entry-point name ≠ `reasoner_id`, duplicate id, or no adapter for `--reasoner-id` |

All errors are logged with structured JSON including `component`, `event_id`, `decision_id` (where known), `error_type`, and `duration_ms`.
