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
│   ├── bundle.py             # DecisionBundle, ReviewPacket, TokenCost
│   └── exceptions.py         # ObservationContractError
├── policy/
│   ├── __init__.py
│   ├── corpus.py             # PolicyDocument, DocumentType, Jurisdiction, RiskTier
│   ├── prompt.py             # PromptTemplate, PromptRegistry protocol
│   ├── gate.py               # PolicyGateOutput, Citation
│   ├── enforcement.py        # EnforcementDecision, EnforcementRule, routing triggers
│   └── exceptions.py         # PolicyGateOutputError, RetrievalError, EnforcementError
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

The policy gate is pluggable. Any reasoning layer that satisfies `PolicyGateProtocol` can replace the LLM. The enforcement and audit layers depend only on `PolicyGateOutput` — they do not know or care what produced it.

```python
from typing import Protocol
from core.policy.gate import PolicyGateOutput
from core.decision.scorer import ScorerOutput
from core.policy.retrieval import RetrievalResult
from core.decision.observation import Observation

class PolicyGateProtocol(Protocol):
    """Contract for the reasoning layer.

    Any implementation — LLM, rules engine, ML classifier, human reviewer —
    must satisfy this interface. The enforcement layer calls `reason()` and
    receives a schema-validated PolicyGateOutput.

    The gate never reads domain field names. All domain context arrives
    pre-rendered in GateContext.template_vars by the domain assembler.
    """

    def reason(
        self,
        gate_context: GateContext,
        snippets: list[PolicySnippet],
    ) -> PolicyGateOutput:
        """Produce a schema-valid policy gate output from the given context.

        Args:
            gate_context: Domain-assembled context carrying prompt_template_id,
                pre-rendered template_vars, and retrieval filters. The gate
                resolves the prompt template, substitutes vars, and appends
                the retrieved snippets before calling the LLM.
            snippets: Retrieved policy chunks from the hybrid retrieval layer,
                already filtered by jurisdiction and risk_tier.

        Returns:
            A validated PolicyGateOutput with permitted actions, required
            controls, rationale, and citations.

        Raises:
            PolicyGateOutputError: If the output cannot be produced or
                validated. Callers route to HOLD on this error.
        """
        ...
```

---

## Enforcement Interface

The enforcement layer takes a `PolicyGateOutput` and produces a final `EnforcementDecision`. It is a pure function — no network calls, no I/O. This is what makes deterministic replay possible.

```python
from core.policy.enforcement import EnforcementDecision, EnforcementContext
from core.policy.gate import PolicyGateOutput

def resolve(
    gate_output: PolicyGateOutput,
    context: EnforcementContext,
) -> EnforcementDecision:
    """Apply deterministic enforcement rules to produce the final action.

    Args:
        gate_output: Validated output from the policy gate (reasoning layer).
        context: Contextual flags — novel_entity, adversarial_flag, etc.

    Returns:
        EnforcementDecision with final_action, enforcement_rule_applied,
        override_log, and optional review_packet.

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
    - final_action: DecisionAction
    - required_controls: list[str]
    - latency_ms: float
    - bundle_url: str   # link to full bundle for inspection
```

### Audit Endpoints

```
GET /v1/decisions/{decision_id}
    Returns: Full DecisionBundle

GET /v1/decisions/{decision_id}/replay
    Returns: ReplayResult (final_action, byte_identical: bool, diff_summary)
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
