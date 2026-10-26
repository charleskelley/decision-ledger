# Reasoner

A **reasoner** is a package or service that decides, or proposes, an action
about a subject in one domain and submits it to the ledger. It owns its
events, feature computation, and scoring, and hands the ledger one
`Submission` (`core/submission.py`), in process or over HTTP. The ledger
takes over from there: it validates the submission, resolves its subject,
runs the gates, enforces, and writes the Decision Bundle. The framework
never imports a reasoner (DR-26, DR-31).

A reasoner in this repository is a uv workspace member under
`reasoners/<name>/` that publishes its registration through a
`decisionledger.reasoners` entry point. Six ship here: the reference
reasoner `reasoners/account_takeover/` (import package `ato_reasoner`), the
one domain reasoner; the documented example `reasoners/example/` (import
package `example_reasoner`); and four mocks, one per
[reference reasoner pattern](patterns.md), which demonstrate the contracts
on imaginary domains. A reasoner that runs elsewhere, in any language,
registers through the deployment's registration file or over the API
instead, and submits over HTTP.

This page covers the **abstract** picture: what a reasoner can be, and what
it must provide or have available to hook in. The contract itself, field by
field, is the [Submission contract](reasoner-handoff.md). The reference
implementation is [Account Takeover](account-takeover/account-takeover.md).

---

## What a reasoner is, and is not

A reasoner produces **evidence and a proposal**; the ledger owns the final
action. That separation is what makes governed decisioning possible:

- The reasoner can be probabilistic, opaque, or vendor-supplied.
- The ledger's enforcement is deterministic and auditable.
- The Decision Bundle records both, the reasoner's evidence and the
  ledger's resolution of it, so every decision replays.

A reasoner says what it wants with one of two verbs (DR-30):

- **Record** a decision it made (`mode: "record"`), citing one of its
  registered bypass rules. The ledger checks the citation, re-evaluates the
  rule on the submitted evidence, and runs the mandatory gates.
- **Gate** a decision (`mode: "gate"`), with an optional proposal. The
  ledger runs the mandatory gates, then the advisory gates.

A system that already decides upstream is therefore not a different kind
of thing: it records its decisions. It still does not have the last word.
A mandatory gate can make the action more restrictive, and a cited rule
that does not hold puts a HOLD floor under the record.

---

## Reasoner shapes

The ledger imposes only the `Submission` contract. The reasoning mechanism
inside is the reasoner's choice.

| Shape | Examples | When it fits |
|---|---|---|
| **ML-only** | XGBoost, LightGBM, scikit-learn classifier, neural network | Mature labeled data; bounded output schema; strict latency budget |
| **LLM-only** | Direct LLM classification with structured output; an agent calling the MCP tools | Sparse labeled data; broad domain coverage needed; latency tolerable |
| **Rule-based** | Decision tree, expert system, deterministic feature thresholds | Hard regulatory requirements; explainability dominates; data scarce |
| **Hybrid (ML + LLM)** | Record confident cases under bypass rules; gate ambiguous ones with an LLM gate | Latency budget *and* coverage of edge cases needed |
| **Hybrid (rule + ML)** | Rule-based pre-filter + ML scoring | Regulatory hard constraints; ML refines within them |
| **Human-in-the-loop** | Feature service + analyst review | Low volume, very high stakes |

The ATO reasoner is the ML + LLM hybrid: its XGBoost score is recorded
under one of its two bypass rules when it is confident, and every other
login asks for the RAG policy gate. See
[Account Takeover](account-takeover/account-takeover.md).

A reasoner can combine several shapes inside its own boundary; the ledger
sees only the resulting submission.

---

## What a reasoner must provide

The ledger validates these on every submission and refuses one that breaks
the contract, before any gate runs, with a stable code. The full rules are
in [Intake validation](reasoner-handoff.md#intake-validation).

### Identity

- `reasoner_id`: the registered reasoner.
- `event_id`: unique per event.
- `timestamp`: when the event occurred, timezone-aware.
- `entity_id` and `entity_type`: the reasoner's own id for the subject it
  scored (typically UUID5 of its business key) and that subject's type.
- `native_subject` (optional): the subject in the reasoner's own terms, a
  `NativeSubject(native_namespace, native_subject_id)`. The ledger resolves
  it to the decision's subject through the deployment's identity resolver
  before any gate runs. In a namespace the reasoner's registration
  declares, `entity_id` and `entity_type` must be the ones that declaration
  derives. See [Native subject and declared namespaces](reasoner-handoff.md#native-subject-and-declared-namespaces)
  and [Identity](../identity.md).

### Evidence: `ReasonerContext`

What the reasoner computed, stored verbatim in the bundle so the decision
can be reconstructed without calling the reasoner back:

- `reasoner_id`, `reasoner_name`, `model_version` (and, optionally,
  `model_artifact_sha256` and `inference_latency_ms`): the reasoning
  artifact.
- `label_type`, `label_name`, `label_value`: the prediction (a score or a
  class label).
- `feature_set`: every feature value the reasoner consumed. It must be
  complete: replay, bypass rules, and rules-gate predicates read it.
- `attribution` (optional, strongly recommended): per-event contributions,
  global importance, or a narrative.

An LLM-agent reasoner fills the same fields with what it actually ran
(model id, prompt digest, tool-trace digest), never with placeholders; the
[Submission contract](reasoner-handoff.md#reasoner_context) lists them.

### Gate contexts

One `GateContext(gate_id, gate_config)` for every gate the registration
binds, on **every** submission, recorded ones included. Each gate validates
its own `gate_config` (its `config_model`) and may authorize it per
reasoner. Recorded submissions carry them so any recorded decision can be
evaluated by its advisory gates offline (shadow evaluation).

### The mode and, to record, the bypass citation

- `mode`: `"record"` or `"gate"`.
- `proposed_action`: required to record (it is the decision); optional to
  gate (a floor).
- `bypass`: to record, a `BypassCitation(rule_id, rule_version)` naming a
  registered `BypassRule` whose `action` equals the proposal. The rule's
  predicate is checked by the ledger, not asserted by the reasoner: the
  citation is an id and an optional version pin, and the values the
  predicate read are recorded from the evidence.

A reasoner that uses scores typically registers one bypass rule per
confident band and gates the rest. ATO's rules, for example, are
`allow-band` (`label_value < 0.20` → ALLOW) and `block-band`
(`label_value > 0.95` → BLOCK); the example reasoner's are `low-score` and
`high-score`. A rule-based reasoner might record nearly everything; an
LLM-only reasoner might gate everything, or record under rules over its own
categorical verdict.

### The raw event

`raw_event`: the reasoner's own event as JSON, stored verbatim and never
read by the ledger.

---

## What a reasoner must have available to hook in

### Feature computation

A way to compute the features it consumes. For batch domains they may be
precomputed; for streaming domains (the ATO reference) they are computed
online from the inbound event and prior state. The ledger prescribes no
feature store: ATO uses Redis sliding windows from its own `compose.yaml`
(`ato_reasoner/feature_service.py`); another domain might use DataFrames,
SQL, or a managed store. What matters is that `feature_set` holds
everything the model consumed.

### A score or label

A signal to report as `label_value` and for bypass rules to test. ML
reasoners output probabilities natively; rule-based reasoners can map rule
outcomes to labels; an agent reports its own verdict.

### Explainability

For a useful `attribution`, an introspection mechanism: TreeSHAP for tree
models, integrated gradients for neural networks, attention weights for
transformers, a rule trace for rule engines, or the stated reasoning of an
LLM. The ledger records whatever the reasoner produces; the audit signal is
only as good as the attribution.

### Choosing the mode

The reasoner decides, per decision, whether to record or to gate. With its
registration in hand it can ask
`core.submission.matching_bypass_rule(registration, reasoner_context)`,
which returns the registered rule with the most restrictive action that
holds (the one the ledger would floor the record at anyway), or `None`
when the decision should be gated.

### An assembler

One function that translates the reasoner's own types into the
`Submission`. Everything before it is the reasoner's; everything after it
is the ledger's. The ATO reference is `build_submission` in
`reasoners/account_takeover/src/ato_reasoner/assembler.py`; see
[the assembler pattern](reasoner-handoff.md#the-assembler-pattern-ato-reference).

### Packaging and registration

The ledger knows a reasoner by its `ReasonerRegistration`: the gate set
(`GateBinding`s, mandatory or advisory, each with the gate's configuration
for this reasoner), the bypass rules, the recordable actions, any native
namespaces, and the enforcement profile (the `feature_set` key that marks a
novel entity for Tier 3, and the jurisdiction groups whose policies
conflict for Tier 5). A registration reaches the ledger one of three ways
(DR-33):

- **A plugin.** A workspace member publishes a `ReasonerPlugin`
  (`registration`, `prompts_dir`, `corpus_dir`) under
  `decisionledger.reasoners` and an `EvalAdapter` under
  `decisionledger.eval_adapters`.
- **The registration file.** The deployment lists the registration in
  `DECISIONLEDGER_REGISTRATIONS`; the reasoner installs no code in the
  ledger.
- **The API.** The operator writes the registration with
  `PUT /api/v1/reasoners/{reasoner_id}` and the operator key; the ledger
  admits it as it admits a registration at startup, keeps it in Postgres,
  and serves it from the next decision on. A `PUT` with a changed
  registration replaces it with a new version.

A reasoner has one source: the ledger refuses to start when two sources
register one `reasoner_id`, and refuses a `PUT` for a plugin's or the
file's reasoner (`409 REGISTRATION_SOURCE_IMMUTABLE`). A gate that reads
per-reasoner assets reads a plugin reasoner's from its plugin, and those of
a reasoner registered by file or over the API from wherever the gate
documents: the RAG policy gate reads them from its asset root. The ledger
serves every registration live at `GET /api/v1/reasoners`, each with its
`registration_version` and its source. See [Registration](../registration.md)
and the [new-reasoner checklist](reasoner-handoff.md#new-reasoner-checklist).

### A client and a key

A `LedgerClient` to submit through: `app.client.LocalClient` runs the
ledger in the reasoner's process; `app.client.HttpClient` (or any HTTP
client) calls a ledger service with the reasoner's API key in
`X-DecisionLedger-Key`. The key is configured on the ledger
(`DECISIONLEDGER_API_KEYS`) and writes and reads only that reasoner's
decisions.

---

## Anti-patterns

- **Recording without a rule that holds.** A record must cite a registered
  rule whose action is the proposal, and the ledger re-evaluates it on the
  evidence. A rule that does not hold puts a HOLD floor under the decision
  and is visible in the bundle (`bypass_rule_holds = false`); it is not a
  way around the gates.
- **Citing the lenient rule.** When a stricter registered rule also holds,
  the ledger floors the decision at that rule's action. Pick the rule with
  `matching_bypass_rule`.
- **Omitting a gate context on a record.** Refused (`GATE_CONTEXT_MISSING`):
  shadow evaluation needs every bound gate's context on every submission.
- **An incomplete `feature_set`.** Replay, bypass rules, and rules-gate
  predicates read it; a feature that is not there makes a predicate fail to
  evaluate, and the rule does not hold.
- **A mismatched subject.** Naming a native subject in a declared namespace
  with a different `entity_id` is refused (`NATIVE_SUBJECT_MISMATCH`).
  Derive the id from the declaration, or leave the namespace undeclared.
- **Resolving identity in the reasoner.** The reasoner names what it
  scored; the enterprise's resolver, called by the ledger, decides who that
  is.
- **Mutating evidence after assembly.** The contract's models are frozen;
  mutation would defeat replay.
- **Filler in an agent's `ReasonerContext`.** An optional field the agent
  cannot fill truthfully stays empty.
- **No attribution.** Accepted (some vendor models expose none), but the
  bundle then explains less, and the ledger cannot fix that.

---

## Cross-references

- **[Submission contract](reasoner-handoff.md)**: the wire contract, the
  two modes, the registration, intake validation, the responsibility
  boundary, and the new-reasoner and new-gate checklists.
- **[Identity](../identity.md)**: native subjects, the resolver contract,
  the passthrough, and the entity journal.
- **[Gates](../gates/index.md)**: what a gate is and how one is bound.
- **[Account Takeover](account-takeover/account-takeover.md)**: the
  reference reasoner, its bands and gate path, feature pipeline, and
  tradeoffs.
- **[Scenarios](account-takeover/scenarios.md)**: calibration provenance
  for ATO's training data.
- **[Decision records](../decisions.md)**: DR-11 (the framework and
  reasoner boundary), DR-26 (reasoner packaging and discovery), DR-29 (gate
  sets and the combinator), DR-30 (record and gate), DR-31 (the wire
  submission and the ledger API), DR-32 (the identity contract).
