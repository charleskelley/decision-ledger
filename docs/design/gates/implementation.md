# Gates — How to implement a gate kind

DecisionLedger separates **universal gate contracts** (one set, in `core/gate/`)
from **per-gate-type concrete contracts** (one subpackage per kind, e.g.
`core/gate/policy/` for the LLM-backed policy gate). New gate kinds add a
sibling subpackage. See [DR-20](../decisions.md#dr-20-universal-gate-contracts-per-gate-type-subpackages-closed-discriminated-union-for-mvp)
for the design rationale.

This guide walks through adding a new gate kind end-to-end.

## The universal contracts

Every gate kind subclasses three Pydantic models defined in `core/gate/`:

| Universal contract | Required fields | Purpose |
|---|---|---|
| `GateInput` | `gate_id` | Captures gate-invocation context. |
| `GateOutput` | `gate_id`, `verdict` | Wraps the gate's emitted output. |
| `GateVerdict` | `gate_id`, `permitted_actions`, `required_controls`, `confidence`, `escalate_to_human`, `escalation_reason` | The framework-consumable verdict enforcement reads. |

`gate_id` is the discriminator. Each subclass narrows it to a Pydantic
`Literal[...]` so the framework can pick the right concrete subclass at JSONB
deserialization time.

The universal contracts carry **only** what every gate kind has and what the
framework's enforcement layer reads. Gate-implementation-specific artifacts
(LLM rationale + citations, rule-engine triggered-rule trace, ML feature
contributions, raw text response, token-billing data, etc.) live on
per-gate-type subclasses with typed Pydantic fields. **No `extras: dict[str,
Any]` escape hatches** — typed all the way down.

## Reference: the LLM policy gate

`core/gate/policy/` is the canonical example.

```python
# core/gate/policy/verdict.py
class PolicyGateVerdict(GateVerdict):
    gate_id: Literal["policy"] = "policy"
    rationale: str
    citations: list[Citation]

# core/gate/policy/output.py
class PolicyGateOutput(GateOutput):
    gate_id: Literal["policy"] = "policy"
    verdict: PolicyGateVerdict | None  # narrowed type
    response_text: str | None = None
    token_cost: TokenCost | None = None

# core/gate/policy/input.py
class PolicyGateInput(GateInput):
    gate_id: Literal["policy"] = "policy"
    model_version: str
    prompt_template_id: str
    prompt_template_version: str
    corpus_version: str
    rendered_prompt: str
    prompt_snapshot: PromptSnapshot
    template_vars: dict[str, str]
```

Notice every artifact is a **typed top-level field**. There's no
`config: dict` or `extras: dict`. If a future LLM-gate variant needs an
additional field, it goes here as a typed attribute (or in a sibling
subclass).

The runtime orchestrator lives at `app/gate/policy/` — the OpenAI client
wrapper, prompt registry, error handling. It returns a `GateResult` carrying
`PolicyGateInput`, `PolicyGateOutput`, and `latency_ms`.

## Adding a new gate kind

Suppose you're adding a rule-engine gate (`gate_id="rule"`).

### 1. Create the subpackage

```
core/gate/rule/
├── __init__.py            # re-exports
├── input.py               # RuleGateInput(GateInput)
├── output.py              # RuleGateOutput(GateOutput)
└── verdict.py             # RuleGateVerdict(GateVerdict)
```

Plus the runtime orchestrator:

```
app/gate/rule/
├── __init__.py            # exports your gate class + GateResult
└── gate.py                # RuleGate runtime
```

### 2. Define the typed contracts

Subclass each universal base. Narrow `gate_id` to your kind's Literal. Add
typed fields for the artifacts your gate captures.

```python
# core/gate/rule/verdict.py
class RuleGateVerdict(GateVerdict):
    gate_id: Literal["rule"] = "rule"
    triggered_rules: list[str]   # rule IDs that fired
    rule_engine_version: str

# core/gate/rule/output.py
class RuleGateOutput(GateOutput):
    gate_id: Literal["rule"] = "rule"
    verdict: RuleGateVerdict | None
    evaluation_trace: list[str] = []   # ordered rule-evaluation log

# core/gate/rule/input.py
class RuleGateInput(GateInput):
    gate_id: Literal["rule"] = "rule"
    rules_version: str
    ruleset_id: str
```

If your gate needs sub-records that aren't atomic strings/numbers/lists,
define those as Pydantic models in your subpackage too. Don't fall back to
`dict[str, Any]`.

### 3. Implement the runtime

```python
# app/gate/rule/gate.py
@dataclass(frozen=True)
class GateResult:
    gate_input: RuleGateInput
    gate_output: RuleGateOutput
    latency_ms: float

class RuleGate:
    def evaluate(
        self,
        obs: Observation,
        snippets: list[RetrievedSnippet],  # ignored for rule gate
        *,
        decision_id: str,
        # any kwargs your gate needs (e.g., rules_version)
    ) -> GateResult:
        ...
```

Schema-failure shape: when validation fails (whatever that means for your
kind), return a `GateResult` whose `gate_output.verdict is None`. Enforcement
routes those to HOLD via the schema-failure tier (DR-19).

### 4. Wire the discriminated union

Open `app/audit/store.py` and add your subclass to the deserializer:

```python
# Before:
gate_input = (
    PolicyGateInput.model_validate(data["gate_input"], strict=False)
    ...
)

# After (with rule gate added):
GateInputUnion = Annotated[
    PolicyGateInput | RuleGateInput,
    Field(discriminator="gate_id"),
]
gate_input = (
    TypeAdapter(GateInputUnion).validate_python(data["gate_input"])
    ...
)
```

Same for `GateOutputUnion`. The bundle's typed surface (`gate_input:
GateInput | None`) doesn't change — only the deserializer needs to know
about the new variant.

### 5. Tests

Mirror the test layout:

```
tests/core/gate/rule/
├── __init__.py
├── test_input.py          # field validation, frozen, gate_id Literal
├── test_output.py         # verdict-None case, IS-A GateOutput
└── test_verdict.py        # rule-trace fields, IS-A GateVerdict

tests/app/gate/rule/
├── __init__.py
└── test_gate.py           # runtime helpers (no I/O for unit tests)
```

Add a discriminated-union round-trip test in `tests/app/test_audit_bundle.py`
asserting that a bundle written with `RuleGateOutput` deserializes back as a
`RuleGateOutput` (not the abstract `GateOutput`).

### 6. Documentation

- Note your gate kind in `interface.md` (the gate-protocol section).
- If your gate has notable design choices (e.g., why rules_version is per-call
  vs deployment-pinned), write a DR.

## Things to avoid

- **Don't introduce `dict[str, Any]` fields.** Every artifact your gate
  captures should be a typed Pydantic field. If you find yourself reaching
  for a dict, define a Pydantic submodel and use that.
- **Don't put gate-specific types at `core/gate/` top level.** The top level
  is reserved for universal contracts. Put your types in
  `core/gate/<kind>/`.
- **Don't bypass the universal verdict shape.** `GateVerdict` defines what
  enforcement reads (action/control/confidence machinery). Your subclass
  adds explanation artifacts; it doesn't override or omit universal fields.
- **Don't depend on `app/gate/<kind>/` from `core/`.** The `core/` boundary
  is infra-free. Gate runtime orchestrators (OpenAI clients, rule
  evaluators) live in `app/`; their typed contracts live in `core/`.

## Things that are fine

- A gate kind that doesn't use a corpus omits `corpus_version` from its
  `<Kind>GateInput`. There's no universal "all gates have a corpus"
  assumption.
- A gate kind that's structured-input-only (no rendered prompt) has no
  `rendered_prompt` field. The universal `GateInput` doesn't require text.
- A gate kind that doesn't bill by token has no `token_cost` field on its
  `<Kind>GateOutput`.
- A gate kind whose verdict shape is fully captured by the universal fields
  (no rationale, no citations, no kind-specific extras) can have
  `<Kind>GateVerdict(GateVerdict)` with just the `gate_id` Literal narrowing
  and no other fields. Subclassing-for-discrimination is enough.

## Future work — known tradeoffs and migration paths

### `GateContext.gate_config: dict[str, Any]` (the reasoner→gate transport)

`core/observation/gate_context.py` carries `gate_config: dict[str, Any] |
None` — the only soft-typing escape hatch left in `core/`. The reasoner
populates it; the gate consumes and validates it on its own terms. The
docstring on `GateContext` explains the design: the framework transports
this dict opaquely between reasoner and gate.

**Why it's a dict today:** the reasoner doesn't know which concrete gate
kind will run (the framework dispatches by `gate_id`). Forcing the
reasoner to import `PolicyGateConfig` (or `RuleGateConfig`, etc.) to
populate a typed config object would couple the reasoner to gate-kind
implementations — counter to the layering where reasoners are upstream
of gate dispatch.

**When to revisit:** when a second concrete gate kind ships and the
reasoner has to choose between gate-kind configs, the dict gets harder to
keep correct without typed support. At that point the migration is:

1. Define `GateConfig` (universal base) in `core/gate/`.
2. Define `PolicyGateConfig(GateConfig)` in `core/gate/policy/`,
   narrowing `gate_id: Literal["policy"]` and adding the typed fields the
   policy gate consumes (`template_id`, `template_vars`,
   `jurisdictions`, `risk_tier`).
3. Define each new kind's config in its own subpackage similarly.
4. Change `GateContext.gate_config: dict[str, Any] | None` →
   `GateContext.gate_config: GateConfig | None`. Pydantic
   discriminated-union deserialization picks the concrete subclass by
   `gate_id`.
5. The reasoner-side assembler (e.g.,
   `reasoner/account_takeover/assembler.py:build_observation`)
   constructs the appropriate concrete config — this introduces the
   coupling the dict was avoiding, but it makes the contract type-safe
   and IDE-discoverable.

This would be a DR-22 (or later) move. Don't preemptively land it for a
single gate kind — it's the second-kind problem.

### Adding a third gate kind

The closed-union pattern in `app/audit/store.py` (`PolicyGateInput |
RuleGateInput | …`) handles 2–5 kinds without strain. Past that, consider:

- **Open registry pattern** — gate subpackages self-register their
  concrete classes at import time; the deserializer looks up by
  `gate_id`. Eliminates the central-union edit; introduces runtime
  indirection and load-order considerations.
- **Per-kind storage tables** — one table per gate kind instead of one
  table with a discriminator column. Simpler per-kind schemas; harder
  cross-kind queries.

Both are DR-grade decisions, not casual refactors.

### Beyond MVP scope (per CLAUDE.md): tooling around gate kinds

If post-MVP you build features like A/B-testing two policy-gate
prompts, region-specific gate variants, or canary deployments, the
extension point is at the orchestrator (`app/main.py`) — pick which
gate instance to invoke per request. The contracts in `core/gate/` don't
need to change. Multiple gate *instances* of the same *kind* still share
the same `gate_id` and concrete subclass; instance selection is an
operational concern, not a framework-contract concern.
