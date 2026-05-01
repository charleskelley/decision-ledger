# Interface

The `core/` module boundary, public contracts between pipeline components, and the external API surface.

---

## The `core/` Boundary

`core/` is the only module in the repository with no infrastructure dependencies. Everything else — `app/`, `eval/`, `generator/` — imports from `core/`. Nothing imports from `app/` except `app/` itself.

**What lives in `core/`:**
- Pydantic models for all shared data types (`LoginEvent`, `FeatureVector`, `DecisionBundle`, etc.)
- Enums and constants (`DecisionAction`, `Jurisdiction`, `DocumentType`)
- Protocol definitions for pluggable reasoning layers
- Exception types (`PolicyGateOutputError`, `ScorerInferenceError`, etc.)
- Pure functions with no side effects (action severity ordering, idempotency key computation)
- Evaluation metric interfaces and dimension contracts

**What does NOT live in `core/`:**
- Redis clients or any network I/O
- SQLAlchemy, asyncpg, psycopg, or any database driver
- OpenAI SDK, anthropic SDK, or any LLM client
- FastAPI, Starlette, or any HTTP framework
- sentence-transformers, XGBoost, or any ML library

The test: if adding an import to `core/` requires `docker compose up` to pass, it does not belong there.

---

## Package Layout

```
core/                         # Framework contracts — no infrastructure, no domain types
├── decision/
│   ├── __init__.py
│   ├── actions.py            # DecisionAction enum, GateRouting enum, severity ordering
│   ├── observation.py        # Observation protocol, validate_observation
│   ├── reasoner_context.py   # ReasonerContext, Signal, LabelType, AttributionSummary
│   ├── gate_context.py       # GateContext
│   ├── bundle.py             # DecisionBundle
│   ├── snippet.py            # RetrievedSnippet (universal retrieved corpus chunk)
│   ├── enforcement.py        # EnforcementDecision (enforcement-layer output)
│   ├── routes.py             # GateRoute (top-level routing enum)
│   ├── resolution.py         # ResolutionAttempt, ResolverKind, ResolutionStatus
│   └── exceptions.py         # ObservationContractError
├── gate/
│   ├── __init__.py
│   ├── input.py              # GateInput (universal base)
│   ├── output.py             # GateOutput (universal base)
│   ├── verdict.py            # GateVerdict (universal base)
│   ├── corpus.py             # PolicyDocument, DocumentType
│   ├── routes.py             # GateRoute enum
│   ├── exceptions.py         # PolicyGateOutputError, RetrievalError, EnforcementError
│   └── policy/               # LLM-backed policy gate concrete contracts
│       ├── __init__.py
│       ├── input.py          # PolicyGateInput(GateInput)
│       ├── output.py         # PolicyGateOutput(GateOutput), TokenCost
│       ├── verdict.py        # PolicyGateVerdict(GateVerdict)
│       ├── citation.py       # Citation
│       └── prompt.py         # PromptTemplate, PromptSnapshot, PromptRegistry
└── eval/
    ├── __init__.py
    ├── dimensions.py         # EvalDimension enum, DimensionResult
    ├── metrics.py            # Metric interfaces: precision, recall, MRR, faithfulness, etc.
    └── exceptions.py         # EvalDimensionError, ThresholdViolation

reasoner/                     # ATO Reasoner domain layer — no infrastructure, imports from core/
└── account_takeover/
    ├── __init__.py
    ├── events.py             # LoginEvent, GeoData, AuthMethod, AuthOutcome
    ├── features.py           # AtoFeatureVector, WindowSpec
    ├── scorer.py             # ScorerOutput
    ├── policy.py             # Jurisdiction, RiskTier (ATO-level policy enums)
    └── assembler.py          # build_observation() — the domain → framework boundary
```

> **Note:** This layout will be fleshed out further as `app/policy_gate/`,
> `app/enforcement/`, and `app/audit/` are built. Treat it as a current-state
> snapshot, not a comprehensive interface specification.

---

## Reasoning Layer Protocol

The gate is pluggable. Any reasoning layer that produces typed
`GateInput` / `GateOutput` subclasses (with a valid `GateVerdict`
subclass inside `GateOutput.verdict`) can replace the LLM policy gate.
The enforcement and audit layers depend only on the universal
`GateVerdict` base — they do not know or care what produced it.

Per DR-20, each gate kind subclasses the universal contracts in its own
subpackage (`core/gate/<kind>/`). See [`gates.md`](./gates.md) for the
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

See [`gates.md`](./gates.md) for the full guide. In brief: subclass the
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
from core.bundle import EnforcementDecision
from core.gate import GateVerdict

def resolve(
    obs: Observation,
    gate_output: GateVerdict | None,  # bundle.gate_output.verdict at replay time
    *,
    snippets: list[RetrievedSnippet],
    decision_id: str,
) -> EnforcementDecision:
    """Apply deterministic enforcement rules to produce the final action.

    Args:
        obs: Assembled Observation from the domain reasoner.
        gate_output: Validated GateVerdict, or None on schema-failure /
            fast path.
        snippets: Policy snippets used by the gate (for tier-5 jurisdiction
            conflict detection).
        decision_id: Decision UUID for log context.

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

The external API is minimal for MVP — it is a thin layer over the pipeline, not a product surface.

<!-- TODO: Document full FastAPI route definitions once the app layer is implemented. -->
<!-- Planned endpoints: -->

### Decision Endpoint

```
POST /v1/decisions

Request body: LoginEvent (JSON)
Response: DecisionResponse
    - decision_id: str
    - decision_action: DecisionAction   # the pipeline's verdict (immutable)
    - required_controls: list[str]
    - latency_ms: float
    - bundle_url: str   # link to full bundle for inspection
```

### Audit Endpoints

```
GET /v1/decisions/{decision_id}
    Returns: Full DecisionBundle

GET /v1/decisions/{decision_id}/replay
    Returns: ReplayResult (decision_action, actions_match: bool, diff_summary)

GET /v1/decisions/{decision_id}/resolution
    Returns: { resolution_status, realized_action, attempts: list[ResolutionAttempt] }
    For non-terminal decision_action only; terminal actions trivially have
    realized_action == decision_action.
```

### Health and Version

```
GET /healthz
    Returns: {"status": "ok", "model_version": "...", "prompt_version": "...", "corpus_version": "..."}
```

---

## Error Types

All exception types are defined in `core/` and imported by `app/`. Infrastructure-specific exceptions (e.g., `redis.ConnectionError`) are caught at the `app/` boundary and re-raised as `core/` types.

| Exception | Module | When raised |
|-----------|--------|-------------|
| `FeatureComputationError` | `core.decision.exceptions` | Feature window computation fails |
| `ScorerInferenceError` | `core.decision.exceptions` | XGBoost inference error |
| `RetrievalError` | `core.policy.exceptions` | Both dense and sparse retrieval fail |
| `PolicyGateOutputError` | `core.policy.exceptions` | LLM output fails schema validation |
| `EnforcementError` | `core.policy.exceptions` | Enforcement rule evaluation error |
| `BundleWriteError` | `core.decision.exceptions` | Audit store write failure |
| `ThresholdViolation` | `core.evaluation.exceptions` | Eval dimension fails CI gate |

All errors are logged with structured JSON including `component`, `event_id`, `decision_id` (where known), `error_type`, and `duration_ms`.
