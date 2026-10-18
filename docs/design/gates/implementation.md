# How to implement a gate

A gate is a uv workspace member under `gates/<name>/` that publishes one
`GatePlugin` through the `decisionledger.gates` entry-point group. The
framework discovers it, builds it once per process, and calls it for every
decision whose gate set includes it. This page is the contract: what a gate
declares, what it receives, what it must return, what the framework checks,
and how its output is enforced and stored. The step list for adding one is
the [new-gate checklist](../reasoners/reasoner-handoff.md#new-gate-checklist);
the two reference members, `gates/rules` and `gates/policy_rag`, are the
worked examples ([below](#worked-examples)).

The contract lives in two places: the universal types in `core/gate/` and
`core/gate_request.py` (pure Pydantic models), and the plugin seam in
`app/gates.py`. The design is
[DR-28](../decisions.md#dr-28-gate-contract-and-gate-plugins) (the open
contract) and
[DR-29](../decisions.md#dr-29-gate-sets-and-the-conjunction-combinator)
(gate sets, fail-closed invocation, budgets).

## The plugin and the gate

The entry point is named after the gate's id:

```toml
# gates/<name>/pyproject.toml
[project.entry-points."decisionledger.gates"]
rules = "rules_gate.plugin:PLUGIN"
```

`PLUGIN` satisfies `app.gates.GatePlugin`:

| Attribute | Meaning |
|---|---|
| `gate_id: str` | The id gate bindings and gate contexts name. Must equal the entry-point name. |
| `kind: GateKind` | `llm`, `lookup`, or `rules`. Descriptive: reported by `GET /api/v1/gates`, not read by enforcement. |
| `deterministic: bool` | `True` when the gate is a pure function of its request. Enforcement skips Tiers 2 and 4 for it. |
| `config_model: type[BaseModel] \| None` | Validates the `gate_config` a reasoner sends, at intake. `None` when the gate validates on consumption. |
| `build(framework) -> Gate` | Builds the runtime gate against the shared `FrameworkServices`. |

`discover_gates` loads every entry point in the group, sorted by name, and
refuses (with `GatePluginError`, at startup) an entry point that fails to
load, an object that does not satisfy the protocol, a `kind` outside
`GateKind`, a non-bool `deterministic`, a `config_model` that is not a
Pydantic model, a name that differs from `gate_id`, or two plugins with one
id.

`build` receives `FrameworkServices`, among them `settings` (the framework
settings, including the Postgres DSN), `pg_conn`, `store`, `registry`, and
`reasoners` (the composed reasoner plugins, whose `prompts_dir` and
`corpus_dir` a gate may read). The `gates` map is empty while gates are being built: a gate never
reaches another gate. `build_gates` refuses an object that does not satisfy
`Gate`, including one whose `evaluate` is not a coroutine function.

The built object satisfies `app.gates.Gate`:

```python
class Gate(Protocol):
    async def evaluate(self, request: GateRequest) -> GateOutput: ...
    def close(self) -> None: ...
```

The framework closes every composed gate on shutdown, in reverse
composition order.

## What a gate receives

`core.gate_request.GateRequest` is the gate's entire view of a decision:

| Field | Meaning |
|---|---|
| `decision_id` | The decision being made; for log correlation only. |
| `submission` | The validated `Submission`. A gate reads its `reasoner_context` (`label_value`, `feature_set`, the reasoner's id); `raw_event` is the reasoner's own and opaque. |
| `gate_context` | The `GateContext` addressed to this gate: its `gate_config`, already validated against `config_model`. An empty context for a gate the deployment mandates that the reasoner does not bind. |
| `budget_ms` | The wall-clock budget for this call, enforced by the framework. |
| `identity` | The decision's subject as the ledger resolved it, before any gate ran ([Identity](../identity.md#the-identity-step)). A gate about a person (a do-not-contact list, a sanctions screen) keys on it rather than on `submission.entity_id`. `None` only when shadow evaluation re-runs a bundle written before the identity contract. |

A request never carries another gate's output, and every gate in a gate set
sees the same submission.

## What a gate returns

`core.gate.GateOutput` is open
([DR-28](../decisions.md#dr-28-gate-contract-and-gate-plugins)): a typed
universal base the framework interprets, and an opaque extension it stores
verbatim and never reads.

| Field | Read by | Meaning |
|---|---|---|
| `gate_id` | framework | The gate that ran. |
| `deterministic` | enforcement | Must equal the plugin's declaration; stored so replay applies the same tiers. |
| `verdict: GateVerdict \| None` | enforcement | The verdict. `None` means the gate ran but reached no valid verdict, which enforcement holds (Tier 1). When set, `verdict.gate_id` equals `gate_id`. |
| `evidence: tuple[EvidenceRef, ...]` | enforcement (Tier 5) | References to the sources the gate relied on. |
| `evidence_snapshot: JsonValue \| None` | nobody | Everything the gate consulted to reach its verdict, so the bundle is self-contained for audit. |
| `extra: dict[str, JsonValue]` | nobody | Gate-specific output fields: a rationale, a raw response, token cost, a failure reason. |

`GateVerdict` carries only what enforcement reads: `permitted_actions`
(enforcement takes the most conservative), `required_controls` (matched
against sentinel values, below), `confidence` in [0.0, 1.0],
`escalate_to_human`, and `escalation_reason`. `EvidenceRef` is
`source_id`, an optional `locator`, and `jurisdictions` (a set of tags; empty
for a source with no jurisdiction). All of these models are strict, frozen,
forbid unknown fields, and reject non-finite numbers.

A gate keeps its own typed models in its own package and serializes them
into `evidence_snapshot` and `extra` with `model_dump(mode="json")`; a reader
that knows the gate re-validates them. A gate may use a richer model
internally (the RAG gate's `PolicyGateVerdict` is its LLM structured-output
schema) as long as what it returns is the universal base: the framework
never defines, imports, or deserializes a subclass.

### What the framework checks

`app.gates.invoke_gate` runs one gate and never raises for the gate's own
failure. It records the gate as `failed`, with a reason, when:

- no gate is composed under the context's `gate_id` (`not_composed`);
- the gate is still awaiting at the deadline, or returns after its budget
  (`timeout`);
- the gate raises, including a `TimeoutError` or `CancelledError` of its own
  (`error`; only the caller's own cancellation propagates);
- the output is not exactly a `GateOutput`, its verdict not exactly a
  `GateVerdict`, or its evidence not exactly `EvidenceRef`s; it names another
  gate; it misstates `deterministic`; or the ledger could not store it as
  returned: a NUL character, a non-finite number, invalid Unicode, or a value
  that does not survive the JSON round trip (`malformed_output`).

A failed gate fires Tier 1 with its reason, so the decision is held for
review and still written. A reason that quotes the gate's own text has NUL
and unencodable characters escaped. These checks exist so that the bundle
the caller receives equals the bundle the store reloads.

## Configuration

A reasoner configures a gate through the `gate_config` of its `GateContext`
(a JSON object). The framework transports it and reads none of its keys.

- **`config_model`.** When the plugin declares one, the ledger validates
  every context addressed to the gate against it at intake, in both modes
  (recorded decisions too, so their contexts stay evaluable for shadow
  evaluation) and on every route (`POST /api/v1/decisions`, the batch route,
  `LocalClient`). A failure is `422 GATE_CONFIG_INVALID` before any gate
  runs. The gate parses the same model again in `evaluate`. `GET
  /api/v1/gates` publishes its JSON schema as `config_schema`, so an HTTP
  reasoner can build contexts without the gate's code. Forbid unknown keys,
  so a misspelled key fails at intake instead of being ignored.
- **`ConfigAuthorizer`** (optional capability of the built gate). A gate that
  defines `authorize_config(config, *, reasoner_id)` is asked at intake,
  right after `config_model` validation, whether this reasoner may send this
  configuration. `config` is the `config_model` instance, or the raw mapping
  when there is none. A `ValueError` is `422 GATE_CONFIG_UNAUTHORIZED`.
- **`ReasonerScope`** (optional capability of the built gate). A gate that
  defines `serves(reasoner_id)` is asked at startup for every registration
  that binds it. A gate that loads per-reasoner assets when it is built can
  serve only the reasoners whose assets it loaded; answering `False` refuses
  the deployment instead of refusing every submission of that reasoner.

Both capabilities belong to the built gate, not the plugin, because what
they decide often exists only after `build` has seen the composed
reasoners. A gate the deployment mandates (`DECISIONLEDGER_MANDATORY_GATES`)
for a reasoner that does not bind it runs with an empty configuration;
startup checks that its `config_model` validates `{}` and, when it is a
`ConfigAuthorizer`, that it authorizes it for that reasoner.

## Budgets

Each invocation runs under `request.budget_ms`, which is
`DEFAULT_GATE_BUDGET_MS` (45,000 ms, `app/submit.py`) for every live
decision. The budget is cooperative: the gate runs inline in its own task
under `asyncio.timeout`, which can cancel it only at an `await`. A gate that
blocks the event loop past its budget, or ignores its cancellation, is
failed when it returns. The clock covers this gate's own evaluation only.

So `evaluate` must not block the loop: run blocking I/O or CPU-bound work in
a thread (`asyncio.to_thread`) and await it, and plan internal timeouts
(model calls, retries) within `budget_ms`. Mandatory gates run on every
decision, so keep them cheap. The reference RAG gate does not yet follow the
first rule: its retrieval is synchronous, a gate-owned follow-up recorded in
DR-29.

## Enforcement of one gate

`app.enforcement.resolver.resolve_gate_output` applies the gate-level tiers
to the output of every gate that ran or failed, mandatory and advisory
alike. They run in order and the first that fires decides the gate's
enforced action:

| Tier | Fires when | Action | Skipped when |
|---|---|---|---|
| 1 Schema failure | No output (the gate failed) or `verdict is None` | HOLD | never |
| 2 Adversarial probe | `required_controls` contains `adversarial_probe_detected` | BLOCK | the gate is deterministic |
| 4 Low confidence | `confidence < 0.60` and the most conservative permitted action is HOLD or BLOCK | HOLD | the gate is deterministic |
| 5 Jurisdiction conflict | `required_controls` contains `jurisdiction_conflict`; or the gate escalates to a human with a reason mentioning "jurisdiction" or "conflict"; or the evidence's jurisdictions span two or more of the registration's `jurisdiction_conflict_groups` | HOLD | the evidence carries no jurisdictions |
| 6 Default | none of the above | the most conservative permitted action (HOLD if none is permitted) | never |

Each gate's result is an `EnforcementDecision`: the action, the tier that
fired (`tier1_schema_failure`, `tier2_adversarial_probe`,
`tier4_low_confidence`, `tier5_jurisdiction_conflict`, or `None` for the
default), and a log line per tier checked. The thresholds and sentinels are
constants in the resolver.

The decision-level rules then apply once per decision
(`resolve_decision`): Tier 3 (novel entity) on a gated decision, the
bypass-rule checks on a recorded one, and the combinator, which takes the
most restrictive of the proposal, every mandatory gate's action, the advisory
side, and any bypass floor. A gate does not need to know about them; see
[Gates](index.md#how-the-ledger-combines-gates) and
[DR-29](../decisions.md#dr-29-gate-sets-and-the-conjunction-combinator).

## What the bundle stores

The bundle records one `core.bundle.GateRecord` per gate in the decision's
gate set:

| Field | Meaning |
|---|---|
| `gate_id`, `mandatory` | The gate, and whether it was mandatory for this decision (bound mandatory or mandated by the deployment). |
| `status` | `ran`, `skipped`, or `failed`. |
| `reason` | Why it was skipped or failed; `None` when it ran. |
| `input` | A `GateInput` (just `gate_id`): the framework's record that it invoked the gate. `None` when it did not. |
| `output` | The `GateOutput` as returned, opaque extension included. `None` unless it ran. |
| `enforcement` | The gate-level `EnforcementDecision` (the fail-closed HOLD for a failed gate). `None` when it was skipped. |
| `duration_ms` | Wall-clock time of the call, when the gate was invoked. |

The latency breakdown records `gate_ms` for the gate phases as a whole.
Replay re-runs both levels of enforcement on these records under the
registration snapshot the bundle cites; it never re-invokes a gate and reads
no corpus or index, which is why everything a gate consulted belongs in
`evidence_snapshot`.

## Owning infrastructure

A gate owns everything it runs on
([DR-28](../decisions.md#dr-28-gate-contract-and-gate-plugins)); the
framework's only infrastructure is Postgres.

- **Services.** A `compose.yaml` in the member, which the root
  `docker-compose.yml` pulls in with `include:` (the RAG gate's
  Elasticsearch).
- **Postgres tables.** The gate's own schema inside the framework's
  database, with DDL shipped in the member (`src/<package>/sql/`), applied
  idempotently by the gate (the RAG gate applies it on every build). Gate DDL
  never creates or alters anything in the framework's `decisionledger`
  schema (`tests/test_framework_boundary.py` checks), and `make lint-sql`
  lints every gate's DDL with the framework's.
- **Make targets.** A `gate.mk` the root Makefile includes
  (`-include $(wildcard gates/*/gate.mk)`). Targets are suffixed with the
  gate directory name (`prepare-gate-<name>`, `eval-gate-<name>`) and append
  themselves to `GATE_PREPARE_TARGETS` and `GATE_EVAL_TARGETS`, which
  `make prepare-gates` and `make eval` fan out over, and to `GATE_HELP` for
  `make help`.
- **Settings.** A `pydantic-settings` class with the gate's own prefix
  (`RULES_GATE_`, `POLICY_RAG_GATE_`), reading the same `.env` as the
  framework, read lazily in `build` so importing the plugin touches nothing.
- **Dependencies.** In the member's `pyproject.toml` only; the root package
  never depends on a gate. The root `gates` dependency group installs the
  reference members.

A gate member is reasoner-agnostic: it imports no reasoner and names no
reasoner's vocabulary (the boundary test scans its source, prompts, DDL, and
mappings). Anything per reasoner comes from the reasoner: its plugin's
assets, its gate contexts, or its eval datasets.

## Owning eval

A gate's quality dimensions belong to the gate, not to the framework
harness. The RAG gate's harness (`python -m policy_rag_gate.eval`) shows the
pattern: it builds its own dimensions, reads the datasets each reasoner
ships for the gate from that reasoner's `EvalAdapter.datasets_dir`, and runs
them through the framework's `eval.runners.harness.run_eval`, so it writes
the same `EvalReport` schema, staged under
`outputs/stage/eval/gate/<gate_id>/<reasoner_id>/`. See
[Evaluation](../evaluation.md).

## Tests

A member's `tests/` (no `__init__.py`) runs under the root `pytest`, which
collects `gates/*/tests`. The reference members' suites import nothing from
the framework's `tests/` tree: the RAG gate's tests run on copies of a stub
prompt and corpus under `gates/policy_rag/tests/fixtures/`. Unit tests need no
Docker; integration tests that need the gate's own services are marked
`integration`. An LLM-backed gate is not unit-tested with mocked model
responses; its eval harness is its test suite.

Framework tests never need a real gate: they compose `tests/_fake_gate`, a
test-only gate whose outcomes (actions, confidence, controls, evidence
jurisdictions, a schema failure, an error, a delay) are scripted by its
`gate_config`.

## Worked examples

### The rules gate

`gates/rules` (package `rules_gate`) is the smallest real gate: `kind`
`rules`, deterministic, no infrastructure.

- **Plugin.** `RulesGatePlugin` declares `config_model = RulesGateConfig`
  (`ruleset_id`, strict, unknown keys forbidden). `build` loads every rule-set
  YAML file under `RULES_GATE_RULESETS_DIR` and ignores the framework
  services.
- **Evaluate.** It looks up the rule set the context names and checks that
  the rule set belongs to the submitting reasoner. It evaluates every rule's
  predicate (the framework's closed predicate language,
  `core.gate.predicate`) against `reasoner_context`, and permits the actions
  of every matching rule, or the rule set's `default_action` when none
  matches, with confidence 1.0.
- **Output.** One `EvidenceRef` per matching rule (`source_id`
  `<ruleset_id>@<version>`, `locator` the rule id), or one for the default;
  no jurisdictions, so Tier 5 is skipped. `evidence_snapshot` is a
  `RulesGateSnapshot`: the rule set as evaluated, the matching rule ids, and
  the observed value of every field the rules reference.
- **Failing closed without raising.** An unknown rule set, a rule set owned
  by another reasoner, or a predicate that cannot evaluate returns
  `verdict=None` with `extra = {"failure": ..., "detail": ...}`, so the
  bundle keeps the forensics.

See [`gates/rules/README.md`](https://github.com/charleskelley/decision-ledger/blob/main/gates/rules/README.md).

### The RAG policy gate

`gates/policy_rag` (package `policy_rag_gate`) is the full shape: `kind`
`llm`, non-deterministic, its own Postgres schema, Elasticsearch, models,
LLM adapters, and eval.

- **Plugin.** `build` derives a `ReasonerProfile` per composed reasoner from
  the prompts and corpus it ships, loads every reasoner's prompt templates,
  opens its own Postgres connection and applies its DDL, and builds the
  retriever and the LLM client.
- **Capabilities.** The built `PolicyGate` is a `ConfigAuthorizer` (a
  reasoner may render only its own templates and filter only to
  jurisdictions its corpus covers) and a `ReasonerScope` (it serves only the
  reasoners it built a profile for).
- **Output.** The universal part of the model's verdict, one `EvidenceRef`
  per retrieved snippet carrying the snippet's jurisdiction, a
  `PolicyGateSnapshot` (the retrieval and the exact prompt) as
  `evidence_snapshot`, and a `PolicyGateExtra` (rationale, citations, token
  cost, retrieval latency, the failing stage) as `extra`.

See [Policy gate](policy/index.md).

## Things to avoid

- **Returning a subclass.** Return exactly `GateOutput`, `GateVerdict`, and
  `EvidenceRef`; anything else fails the gate closed. Put gate-specific
  fields in `evidence_snapshot` or `extra`.
- **Putting gate types in `core/`.** `core/gate/` holds the universal
  contracts only; a gate's own models live in its member.
- **Blocking the event loop.** A blocking `evaluate` cannot be cancelled at
  its deadline and delays every concurrent gate.
- **Raising for an expected failure.** Raising also fails closed, but the
  record keeps only the exception text. Returning `verdict=None` with the
  reason in `extra` keeps the gate's own forensics in the bundle.
- **Depending on another gate.** Gates in a set are independent and run
  concurrently; there is no order and no data flow between them.
- **Writing outside your own schema,** or naming a reasoner in the member.

## Things that are fine

- A gate with no `config_model` validates on consumption;
  `authorize_config`, if it has one, then receives the raw mapping.
- A gate whose evidence carries no jurisdictions: Tier 5 is skipped for it.
- A gate with nothing to snapshot (`evidence_snapshot=None`) or no extra
  fields.
- A gate with no infrastructure at all: no compose file, no schema, no
  `gate.mk` (the rules gate).
- A deterministic gate that reports confidence 1.0: Tiers 2 and 4 are
  skipped for it, so confidence never decides its enforcement.

## Future work

- **The RAG gate's blocking calls.** Its retrieval runs synchronously inside
  `evaluate`, and a model call its provider SDK retries can outlast the gate
  budget. Moving retrieval off the event loop and sizing retries to
  `request.budget_ms` are gate-owned follow-ups (DR-29).
- **Per-binding configuration.** A declared per-reasoner allow-list (for a
  reasoner that ships a template it may not use yet) could return as
  configuration on the `GateBinding` (DR-28, Phase 1b judgment calls).
