# Submission contract

A reasoner hands the ledger one thing: a `Submission`
(`core/submission.py`), the wire contract DR-31 defines. It either asks the
ledger to **record** a decision the reasoner made or to **gate** one it
proposes (DR-30). Everything before the submission is the reasoner's:
events, features, scoring, and choosing the mode. Everything after it is
the ledger's: intake validation, the decision's subject, the gate set,
enforcement, the combinator, and the Decision Bundle. Read this page before
changing a reasoner's features, its scorer, or a gate.

The submission is concrete, strict, frozen, and JSON-serializable. No
reasoner-specific Python type crosses the boundary, so an in-process
caller, an HTTP client, and an agent calling an MCP tool hand over the same
thing, and replay needs no reasoner code.

![Submission flow: on the reasoner side, events become features and a score, and build_submission records under the strictest registered bypass rule that holds or asks for the gate. The Submission (wire schema version 2) reaches the ledger through LocalClient in process or HttpClient over HTTP. The ledger validates it, resolves its subject, runs the mandatory gates, then the advisory gates (gate mode only, skipped once a mandatory gate blocks), applies enforcement and the combinator, writes the version-2 bundle, and returns a receipt. In record mode the advisory gates are recorded as skipped and the cited bypass rule is checked.](../../assets/diagrams/reasoner-handoff.svg)

---

## Where a submission goes

There is one operation, reached two ways:

| Path | How | Who authenticates |
|---|---|---|
| Over HTTP | `POST /api/v1/decisions` with the submission as the JSON body | The reasoner's API key in `X-DecisionLedger-Key` |
| In process | `app.client.LocalClient.submit(submission)` | The trusted in-process principal |

Both run `app.api.decisions.create_decision`, which parses the body
strictly (`Submission.model_validate_json`), checks that the caller may
write for `reasoner_id`, and calls `app.submit.submit`. `LocalClient`
serializes the submission to the same JSON first, so a decision made in
process and one made over HTTP go through identical code.
`app.client.HttpClient` is the matching HTTP client; both clients satisfy
`LedgerClient`, return the same wire models (`app/api/models.py`), and
raise the same `core.exceptions.LedgerError` (an HTTP `status` and a stable
`code`).

A write returns **201** with a `DecisionReceipt` and a `Location` header.
The receipt is the outcome, not the record (DR-16): `decision_id`,
`reasoner_id`, `event_id`, `entity_id` and `entity_type` (the subject as
the ledger resolved it), `mode`, `decision_action`, `decision_level_rule`,
`enforcement_rule_applied`, `bypass_rule_id`, `bypass_rule_holds`,
`registration_version`, a summary of every gate, `latency_breakdown`, and
`created_at`. The full bundle is at `GET /api/v1/decisions/{decision_id}`
(`get_bundle` on either client).

`POST /api/v1/decisions:batch` takes NDJSON, one submission per line, and
is record-only: each line is decided like a single record (mandatory gates
included), a bad line is rejected on its own, a line for another reasoner
refuses the whole batch, and the bundles are written in one round trip.

The JSON schema is `schemas/submission-v2.json`, exported from the model by
`app/schemas.py` (`make schemas`); `tests/test_schemas.py` fails when the
committed schema drifts from the model.

---

## The two modes

### Record

`mode: "record"` means the reasoner decided. The submission carries:

- `proposed_action`: the decision (required);
- `bypass`: a `BypassCitation(rule_id, rule_version)` naming one of the
  reasoner's registered bypass rules (required). `rule_version` is an
  optional pin; when set it must equal the registered rule's `version`.

The cited rule's `action` must equal `proposed_action`. The ledger runs the
**mandatory gates only**; every advisory gate is recorded as skipped, with
the reason. It then evaluates the cited rule's predicate against the
submission's `reasoner_context` and snapshots the result in the bundle
(`bypass_check`, `bypass_rule_holds`), together with every other rule the
reasoner registers (`other_bypass_checks`). Two decision-level floors
follow:

- **The rule does not hold** (`bypass_rule_not_satisfied`). The record is
  stored with `bypass_rule_holds = false` under a HOLD floor, which keeps
  the evidence of an unjustified bypass. A predicate that cannot be
  evaluated, such as one reading a missing feature, does not hold. The floor
  never relaxes: a BLOCK record stays BLOCK.
- **A stricter rule also holds** (`bypass_rule_superseded`). The decision
  is floored at that rule's action, so a reasoner cannot cite the lenient
  one of two rules its own evidence satisfies.

The novel-entity tier (Tier 3) does not apply to records. A rule that should
exclude novel entities says so in its predicate.

### Gate

`mode: "gate"` means the reasoner asks the ledger to decide. A gate request
carries no `bypass` citation; `proposed_action` is optional and, when
present, is a floor (the result is never less restrictive than the
proposal). The ledger runs the mandatory gates, then the advisory gates,
unless a mandatory gate already blocks the decision. A proposed BLOCK does
not skip the advisory gates.

On a gated decision about a novel entity (the registration's
`novel_entity_feature` is truthy in `feature_set`), Tier 3 holds the
advisory gates' verdicts for review: their side counts as HOLD, or BLOCK
when an advisory gate's own Tier 2 fired. Mandatory gates and the proposal
still count. A gate-mode submission needs a gate to run: with no bound and
no mandated gate it is refused (`NO_GATES`).

### Combining

The final action is the most restrictive of the proposal, the gates'
enforced actions, and any decision-level floor, over
ALLOW < CHALLENGE < HOLD < BLOCK (DR-29). A gate that raises, outlives its
budget (45 s by default, `DEFAULT_GATE_BUDGET_MS`), returns an unusable
output, or is not composed fails closed: it is recorded `failed` and its
enforcement holds the decision. The [pipeline](../pipeline.md) page walks
through each step.

---

## Fields

Every model in the contract forbids unknown fields at every level and is
strict: parse wire input as JSON text (`Submission.model_validate_json`),
because strict validation accepts JSON's representations (ISO timestamps,
UUID strings, enum values, arrays for tuples) only in JSON mode.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `2` | The wire schema version; always 2. |
| `reasoner_id` | `str` | The submitting reasoner. Must equal `reasoner_context.reasoner_id` and, over HTTP, the reasoner the API key belongs to. |
| `event_id` | `str` | The reasoner's unique id for the event; the default idempotency key and the log correlation id. |
| `timestamp` | timezone-aware datetime | When the event occurred. A naive timestamp is refused. |
| `entity_id` | `UUID` | The reasoner's own id for the subject it scored, typically UUID5 of its native key under its own namespace. |
| `entity_type` | `str` | The type of that subject (`"account"`, `"customer"`, ...). |
| `native_subject` | `NativeSubject` or `null` | The subject in the reasoner's own terms: `native_namespace` and `native_subject_id`. See [Native subject](#native-subject-and-declared-namespaces). |
| `mode` | `"record"` or `"gate"` | See [The two modes](#the-two-modes). |
| `proposed_action` | `ALLOW`, `CHALLENGE`, `HOLD`, `BLOCK`, or `null` | Required to record; an optional floor to gate. |
| `bypass` | `BypassCitation` or `null` | Required exactly when `mode` is `"record"`. |
| `reasoner_context` | `ReasonerContext` | The reasoner's evidence. |
| `gate_contexts` | list of `GateContext` | One per bound gate, each `gate_id` at most once. |
| `raw_event` | JSON object | The reasoner's own event, stored verbatim and never interpreted by the ledger. |

### `reasoner_context`

`ReasonerContext` (`core/observation/reasoner_context.py`) is what the
reasoner computed. The ledger stores it verbatim inside the stored
submission; gates read it, and bypass rules are evaluated on its
`label_value` and `feature_set`.

| Field | Type | Meaning |
|---|---|---|
| `reasoner_id` | `str` | Equals the submission's `reasoner_id`. |
| `reasoner_name` | `str` | Display name. |
| `model_version` | `str` | The model artifact's version. |
| `model_artifact_sha256` | `str` or `null` | SHA-256 of the artifact at load time; `null` for API or vendor models. |
| `inference_latency_ms` | `float` (≥ 0) or `null` | Inference wall-clock time. |
| `label_type` | `NUMERICAL` or `CATEGORICAL` | Whether the prediction is a score or a class label. |
| `label_name` | `str` | What the prediction is (`"risk_score"`, `"churn_label"`). |
| `label_value` | `float` or `str` | The prediction. |
| `feature_set` | object of `float`, `int`, `str`, `bool` | Every feature the model consumed at inference time. |
| `attribution` | `AttributionSummary` or `null` | `feature_contributions` (per-event, with their `method`), `feature_importance` (global), and `narrative`. |

`feature_set` must be **complete**: it is what makes a decision replayable
from the bundle, and it is what bypass rules and rules-gate predicates read
(`feature_set.<name>`). `attribution` is optional; without it the decision
is still recorded, but its bundle explains less.

**An LLM-agent reasoner** (one that decides with a model call and tools,
for example through the MCP tools) fills the same fields with what it
actually ran, and leaves an optional field empty rather than filling it
with a placeholder:

- `model_version`: the provider's model id exactly as called;
- `model_artifact_sha256`: the SHA-256 of the exact rendered prompt, or
  `null` when no prompt is under the reasoner's control;
- `inference_latency_ms`: the agent's run time up to its proposal;
- `label_type` / `label_name` / `label_value`: the agent's own output that
  its bypass rules read (a `CATEGORICAL` verdict or a `NUMERICAL` score),
  not a restatement of the proposal;
- `feature_set`: the scalar inputs the agent consulted, plus
  `tool_trace_sha256`, the SHA-256 of its ordered tool-call trace;
- `attribution.narrative`: the agent's stated reasoning, verbatim.

### `gate_contexts`

A `GateContext` (`core/observation/gate_context.py`) addresses one gate:
`gate_id` and `gate_config`, a JSON object (or `null`) the ledger
transports without interpreting. The submission must carry exactly one
context for every gate its registration binds, recorded submissions
included (see [Shadow evaluation](#shadow-evaluation)), and none for a gate
the registration does not bind.

Each gate owns its configuration. At intake, in both modes, the ledger
validates `gate_config` against the gate's declared `config_model` and,
when the built gate is a `ConfigAuthorizer`, asks it whether this reasoner
may send that configuration. `GET /api/v1/gates` serves each composed
gate's `config_schema`. A gate the deployment mandates without the
reasoner binding it runs with an empty configuration, and the reasoner
cannot address it. The keys of the reference gates are documented with
those gates: the [RAG policy gate](../gates/policy/index.md) and the
[rules gate](../gates/index.md).

### `raw_event`

The reasoner's event as a JSON object. The ledger stores it verbatim in the
bundle and never reads it: it exists so an auditor sees what the reasoner
saw. Replay does not need it.

### What the ledger can store

The bundle stores the submission verbatim as Postgres `jsonb` and replay
reloads it, so every value must survive that round trip. The submission
refuses non-finite numbers (`NaN`, infinities), NUL characters, and strings
that are not valid Unicode anywhere in it, `raw_event` and `gate_config`
included (`core.submission.storage_problem`). Such a body is a 422
`SUBMISSION_INVALID`.

---

## Native subject and declared namespaces

A submission names the subject it scored in two ways: `entity_id` and
`entity_type` (the reasoner's own id for it) and, optionally,
`native_subject` (the subject in the reasoner's own terms, a namespace and a
key). The decision's subject is neither: it is the identity the ledger
resolves before any gate runs, through the deployment's identity resolver,
and records on the bundle (DR-32). The bundle's identity layer
(`identity.entity_id` in its JSON), the `decision_bundles.entity_id` column,
and the receipt's `entity_id` are that resolved subject; the stored
submission keeps the reasoner's own id.

A reasoner declares each native namespace it uses in its registration
(`native_namespaces`): a `NativeNamespace(native_namespace, namespace_uuid,
entity_type)`. A submission that names a subject in a declared namespace
must carry the `entity_id` that declaration derives
(`derive_entity_id(namespace_uuid, native_subject_id)`, UUID5) and its
`entity_type`, or intake refuses it with 422 `NATIVE_SUBJECT_MISMATCH` (the
native key is not echoed). A namespace the registration does not declare is
not checked. Within a deployment, one namespace name has one declaration.

When no resolver is active, or the active one does not match the
subject, fails, or times out, the passthrough records the subject
unresolved, with the reason: a native subject in a declared namespace is
that declaration's derivation; any other subject is the submission's own
`entity_id` and `entity_type`. A decision is never refused over its
identity. [Identity](../identity.md) covers the resolver contract, the
identity step, the passthrough, and the entity journal.

---

## The registration

A `ReasonerRegistration` (`core/observation/registration.py`) is what the
ledger knows about a reasoner. It is data: the ledger validates every
submission against it, enforcement reads its profile, and replay reads the
snapshot of the version a bundle cites.

| Field | Meaning |
|---|---|
| `reasoner_id`, `reasoner_name` | Identity. `reasoner_id` is the registry key and the bundle store's tenant column. |
| `gates` | The gate set: one `GateBinding(gate_id, mandatory, config)` per gate, each at most once. A mandatory gate runs on every decision; an advisory gate (`mandatory: false`, the default) runs only on a gated decision. The deployment may mandate further gates for every reasoner (`DECISIONLEDGER_MANDATORY_GATES`). `config` (default `{}`) is the gate's configuration for this reasoner: JSON the framework never reads, validated by the gate when it admits the binding (DR-33). |
| `bypass_rules` | `BypassRule(rule_id, version, description, predicate, action)` values a record may cite; `rule_id` is unique. The predicate uses the closed predicate language the rules gate uses (`core/gate/predicate.py`: `compare`, `all`, `any`, `not`) over `label_value` and `feature_set.<name>`. The action is the one action the rule justifies. Empty means the reasoner can only ask for a gate. |
| `recordable_actions` | The actions the reasoner may record; every rule's action must be one of them. Defaults to every action. |
| `native_namespaces` | The declared native namespaces (above). Empty by default. |
| `novel_entity_feature` | Enforcement profile, Tier 3: the `feature_set` key whose truthy value marks a subject with too little history; `null` disables Tier 3. |
| `jurisdiction_conflict_groups` | Enforcement profile, Tier 5: groups of jurisdiction tags whose policies are mutually consistent; a gate's evidence spanning two groups is a conflict. |

A registration's `registration_version` is its content address: the first
16 hex digits of the SHA-256 of its canonical form (sets sorted; a binding
without configuration has no `config` key). Any change to the gate set (a
binding's configuration included), a bypass rule, the recordable actions, a
namespace declaration, or the enforcement profile changes it. The ledger
snapshots every version it serves into `decisionledger.reasoner_registrations`
(at startup, and when a registration is written over the API), and every
bundle records the version it was decided under.

Registrations come from three sources (DR-33; see
[Registration](../registration.md)): an installed reasoner plugin
(`ReasonerPlugin.registration`), the deployment's registration file,
`DECISIONLEDGER_REGISTRATIONS` (YAML, a list of registrations in their JSON
shape, validated strictly), and `PUT /api/v1/reasoners/{reasoner_id}` with
the operator key (the same JSON shape). A reasoner has one source: one
registered by two refuses startup, and a plugin's or the file's reasoner
cannot be replaced over the API (409). The ledger serves each with its
version and source (`plugin:<module>`, `file:<path>`, or `api`):
`GET /api/v1/reasoners`, `GET /api/v1/reasoners/{reasoner_id}`, and
`GET /api/v1/reasoners/{reasoner_id}/registrations/{version}`.

---

## Intake validation

All of these run before any gate, and nothing is persisted when one fails.
A `SubmissionContractError` (`core/exceptions.py`) is rendered as an
RFC 9457 problem document with status 422, its stable `code`, and the
`reasoner_id`. In order:

1. **The request.** No or an unknown key is 401 `UNAUTHENTICATED`. A body
   over 1 MiB is 413 `BODY_TOO_LARGE`.
2. **The shape** (the `Submission` model): 422 `SUBMISSION_INVALID` for a
   malformed body, an unknown field, a record without a citation or a
   proposal, a gate request with a citation, a `reasoner_context` for
   another reasoner, two contexts for one gate, a naive timestamp, or a
   value the ledger cannot store.
3. **The writer.** A key that belongs to another reasoner is 403
   `REASONER_MISMATCH`; the operator key is 403 `REASONER_KEY_REQUIRED`
   (decisions are written with the reasoner's own key).
4. **The registration.** An unknown `reasoner_id` is 422
   `REASONER_NOT_REGISTERED`.
5. **The citation** (records only, `core.submission.validate_submission`):
   `BYPASS_RULE_UNREGISTERED`, then `ACTION_NOT_RECORDABLE`, then
   `BYPASS_ACTION_MISMATCH`, then `BYPASS_RULE_VERSION_MISMATCH` when the
   citation pins a version.
6. **The native subject:** `NATIVE_SUBJECT_MISMATCH`.
7. **The gate contexts:** `GATE_NOT_BOUND` (a context for a gate the
   registration does not bind), `GATE_CONTEXT_MISSING` (a bound gate with
   no context), and `NO_GATES` (a gate request with no gate to run).
8. **Each gate's configuration** (`app/submit.py`), for every composed gate
   in the decision's gate set: `GATE_CONFIG_INVALID` (it fails the gate's
   `config_model`), then `GATE_CONFIG_UNAUTHORIZED` (the gate's
   `ConfigAuthorizer` refuses it for this reasoner).

`ACTION_NOT_RECORDABLE` is checked before `BYPASS_ACTION_MISMATCH`: a
record whose `proposed_action` is outside the registration's
`recordable_actions` gets that code even when it cites a registered rule.
`LocalClient` raises the same `LedgerError` with the same status and code.

---

## Responsibility boundary

| Responsibility | Reasoner | Ledger | Gate | Identity resolver |
|---|---|---|---|---|
| Event schema, ingestion, feature computation, model, scoring | Owns | — | — | — |
| Choosing the mode and the rule a record cites | Owns (`matching_bypass_rule` helps) | Validates the citation, evaluates every rule, snapshots the checks | — | — |
| Registration: gate set (with each binding's `config`), bypass rules, recordable actions, namespaces, enforcement profile | Declares (plugin or file), or has the operator write it (`PUT`) | Validates and admits it as at startup, versions, snapshots, serves it; keeps the API's registrations; never replaces a declared one | Admits each binding and its `config` (`BindingValidator`) | — |
| `ReasonerContext` | Assembles, complete | Stores verbatim | Reads | — |
| `GateContext` per bound gate | Assembles | Checks the set, transports | Validates (`config_model`) and authorizes its own configuration | — |
| Subject: `entity_id`, `entity_type`, `native_subject` | Names it in its own terms | Checks declared namespaces; calls the resolver under a budget; records the result or the passthrough | Reads the resolved subject (`GateRequest.identity`) | Maps the native subject to the enterprise's subject |
| Identity resolution: matching, crosswalk, master entity index | — | Implements none | — | Owns |
| Gate evaluation and its infrastructure (indexes, model clients, rule files) | — | Invokes under a budget; fails closed | Owns | — |
| Prompts and policy corpus a gate reads | Ships them: with its plugin (`prompts_dir`, `corpus_dir`), or where the gate documents (the RAG gate's asset root) | Carries the plugin to the gate | Loads and indexes them | — |
| Deployment mandates (`DECISIONLEDGER_MANDATORY_GATES`) | — | Owns | — | — |
| Enforcement tiers, decision-level rules, the combinator | — | Owns | — | — |
| Bundle, storage, replay, timelines (`decisionledger.*`) | — | Owns | — | — |
| Evidence escrow and verify | — | Owns the escrow (`evidence_artifacts`) and the verify route; checks that each escrowed artifact is held | Escrows what it relied on (through the store) and verifies its own references (`EvidenceVerifier`) | — |
| API keys and principals | Holds its key | Owns (`app/auth.py`) | — | — |
| Reasoner-only infrastructure and settings (ATO: Redis, `ATO_` prefix) | Owns | — | — | — |
| Eval | Ships an `EvalAdapter`, a driver, and datasets | Runs the framework harness (consistency, robustness, shadow) | Runs its own harness on each reasoner's datasets for it | — |

The ledger trusts the reasoner's evidence as evidence: it checks the
contract, not the domain logic behind a score. There is no import seam
between them. The framework (`core/`, `app/`, `eval/`) never imports a
reasoner package; a reasoner imports `core/` and `app/` (DR-26, DR-31).

---

## Shadow evaluation

Every submission addresses every gate its registration binds, recorded ones
too, and each context passes its gate's `config_model` and authorization at
intake, although a record runs only its mandatory gates. That keeps every
recorded decision evaluable by its advisory gates later, on the exact
evidence it was recorded with, without running the reasoner again.

The framework eval harness's shadow dimension (`eval/dimensions/shadow.py`)
uses it: it samples recorded bundles from the run, turns each stored
submission into a gate request with no proposal, runs the advisory gates on
the stored contexts through the same fail-closed runner live decisions use,
keeps the stored mandatory outcomes, applies the decision-level rules as in
gate mode, and compares the hypothetical action with the recorded one
(agreement, escalation, relaxation, gate failures). Nothing is written to
the ledger. The dimension is report-only until a baseline calibrates a
threshold, and each sample costs one call per advisory gate. See
[Evaluation](../evaluation.md).

A reasoner that omits a bound gate's context is refused
(`GATE_CONTEXT_MISSING`); there is no way to record a decision its gates
could not evaluate.

---

## The assembler pattern (ATO reference)

A reasoner keeps the translation into the contract in one place, its
assembler. The reference is the Account Takeover reasoner's
`build_submission(event, features, scorer, *, template_id)` in
`reasoners/account_takeover/src/ato_reasoner/assembler.py`. It takes ATO's
three domain artifacts (`LoginEvent`, `AtoFeatureVector`, `ScorerOutput`)
and returns one `Submission`; the framework never sees those types. ATO's
pipeline (`ato_reasoner/pipeline.py:run_ato_decision`) computes features,
scores, assembles, and submits through a `LedgerClient`.

Everything below is ATO's, not the framework's:

```
ScorerOutput → ReasonerContext
  reasoner_id, reasoner_name ← ATO_REGISTRATION ("ato-reasoner", "ATO Reasoner")
  model_version              ← scorer.scorer_version
  model_artifact_sha256      ← scorer.scorer_artifact_sha256
  inference_latency_ms       ← scorer.inference_latency_ms
  label_type                 = NUMERICAL
  label_name                 = "risk_score"
  label_value                ← scorer.risk_score
  feature_set                ← the AtoFeatureVector fields as a flat dict
  attribution                ← AttributionSummary(feature_contributions=top_signals)

LoginEvent + AtoFeatureVector + ScorerOutput → GateContext (gate_id "policy")
  template_id                ← AtoSettings.template_id ("ato-v4"; "ato-v3" or "ato-v2" selectable)
  template_vars              ← rendered strings for each placeholder of ato-v4.yaml
  jurisdictions              ← INTERNAL, plus US_FEDERAL for a US login
  risk_tier                  ← HIGH_VALUE at risk_score ≥ 0.60, else STANDARD
  retrieval_query            ← build_ato_query(event, scorer)
  (the corpus is ATO's binding's, ato-policy at tier 1: DR-34)

LoginEvent → subject
  native_subject             = (ato-account, account_id)
  entity_id, entity_type     = the account's id under ATO's declared namespace, "account"

ReasonerContext → mode
  matching_bypass_rule(ATO_REGISTRATION, reasoner_context)
    a rule holds  → mode "record", proposed_action = rule.action,
                    bypass = BypassCitation(rule_id, rule_version=rule.version)
    none holds    → mode "gate", no proposal

LoginEvent → raw_event       = event.model_dump(mode="json")
```

`core.submission.matching_bypass_rule` returns the registered rule that
holds with the most restrictive action (registration order breaks ties),
which is the rule the ledger would floor the decision at if a lenient one
were cited. ATO registers two rules (`ato_reasoner/registry.py`):
`allow-band` (`label_value < 0.20` → record ALLOW) and `block-band`
(`label_value > 0.95` → record BLOCK). Every other login asks for its one
gate, the RAG policy gate, bound advisory. These numbers are ATO's
registration, not framework constants. ATO also declares the native
namespace `ato-account` (subject type `account`), so the passthrough
assigns exactly the `entity_id` ATO's account decisions have always had.
See [Account Takeover](account-takeover/account-takeover.md) for the
reasoner itself.

---

## New-reasoner checklist

A reasoner joins a deployment with no change to the framework. Pick how it
reaches the ledger.

### Over HTTP

For a reasoner that runs as its own service, in any language.

1. **Build submissions.** Produce JSON that satisfies
   `schemas/submission-v2.json`: a complete `reasoner_context`, one gate
   context per gate you bind (each matching that gate's `config_schema`
   from `GET /api/v1/gates`), your event as `raw_event`, and the mode. A
   Python reasoner can build `core.submission.Submission` directly and use
   `matching_bypass_rule` to pick the mode.
2. **Ship your gate assets, if a gate needs them.** A gate that reads
   per-reasoner assets reads a reasoner without a plugin from where that
   gate documents. For the RAG policy gate: your prompt templates and your
   policy corpus under its asset root, `POLICY_RAG_GATE_ASSETS_ROOT`, as
   `<root>/<reasoner_id>/prompts/*.yaml` and
   `<root>/<reasoner_id>/corpus/**/*.md` (template ids are global: one
   another reasoner registers already refuses your binding). Then load the
   corpus, under the corpus id your binding will name, before you register
   (the gate refuses a binding to a corpus that is not loaded, DR-34):
   `uv run python -m policy_rag_gate.corpus_loader --corpus-id <corpus_id>
   --reasoner-id <reasoner_id>` reads `<root>/<reasoner_id>/corpus`. The
   rules gate, a lookup gate, or no gate need nothing here.
3. **Register.** Pick one source (DR-33; see
   [Registration](../registration.md)):
    - **Over HTTP:** the operator sends your registration, in its JSON shape,
      with `PUT /api/v1/reasoners/{reasoner_id}` and the operator key
      (`app.client.HttpClient.register` does it). `201` creates it, `200`
      replaces it with a new version (or finds it unchanged); the ledger
      keeps it in Postgres and serves it after a restart.
    - **By file:** add it to the deployment's registration file
      (`DECISIONLEDGER_REGISTRATIONS`; `conformance/registrations.yaml` is a
      worked example); it changes when the deployment restarts with a new
      file, never over the API.
    - **As a plugin:** publish one and install it in the ledger's
      environment (see [In process](#in-process), steps 1 to 4).

    Either way the ledger admits your registration exactly as it does at
    startup: every gate you bind as mandatory must be composed, and every
    composed gate you bind must accept the binding and its `config`
    (`app.gates.BindingValidator`; the RAG gate checks the corpora your
    binding names, `PolicyBindingConfig`, and reads your assets here) and
    then serve you. A refusal names the gate: the deployment does not start
    with a declared registration a gate refuses, and a `PUT` answers 422
    `GATE_BINDING_REFUSED` and stores nothing.
4. **Reload your corpus** whenever its documents change (the same loader
   command); a load replaces the corpus, and each decision records the
   corpus state it read.
5. **Get a key.** The deployment maps your `reasoner_id` to a key in
   `DECISIONLEDGER_API_KEYS` (comma-separated `reasoner_id:key` pairs) or
   the key file `DECISIONLEDGER_API_KEYS_FILE`; the ledger reads its keys
   when it starts, and registering issues none. Send it as
   `X-DecisionLedger-Key`. Your key writes and reads only your own
   decisions.
6. **Submit** with `POST /api/v1/decisions` (or
   `POST /api/v1/decisions:batch` for records), with any HTTP client or
   `app.client.HttpClient`. Switch on the problem document's `code`, never
   its text. A write is not idempotent on the wire: a retried POST records
   twice.
7. **Check the instance.** Run the conformance suite (`conformance/`,
   `make conformance`) against the ledger you target. It is a black-box
   HTTP suite that checks the contract you rely on (status codes, receipts,
   problem codes, tenant isolation, batch, replay, identity, registration),
   with the example reasoner's payloads as worked requests; see its README
   for the variables it needs.

### In process

For a Python reasoner that lives in this repository as a uv workspace
member and submits through `LocalClient` (or `HttpClient`). Copy
`reasoners/example`, which shows every part of the contract on a small,
imaginary domain:

```
reasoners/<domain>/
├── pyproject.toml          # its own distribution; depends on decision-ledger; two entry points
├── ruff.toml
├── reasoner.mk             # targets suffixed with <domain>, appended to the root aggregates
├── compose.yaml            # infrastructure only this reasoner uses (optional)
├── README.md
├── src/<package>/
│   ├── events.py           # the reasoner's own event
│   ├── scorer.py           # or scorer/: model inference (and training)
│   ├── assembler.py        # build_submission(...) -> Submission
│   ├── pipeline.py         # score, assemble, submit through a LedgerClient
│   ├── registry.py         # the ReasonerRegistration constant
│   ├── plugin.py           # PLUGIN: ReasonerPlugin
│   ├── prompts/            # templates for an LLM-backed gate (immutable once shipped)
│   ├── corpus/             # policy documents for a retrieving gate
│   └── eval/               # ADAPTER: EvalAdapter, a driver, datasets/
└── tests/                  # no __init__.py; collected by the root pytest run
```

1. **Package.** Src layout with a distinct import package, a dependency on
   `decision-ledger` (`[tool.uv.sources] decision-ledger = { workspace =
   true }`), and your own dependencies (add them from the member's
   directory with `uv add`, never to the root `[project] dependencies`).
2. **Register.** Write the `ReasonerRegistration` in `registry.py`: the
   gate set, the bypass rules, the recordable actions, any native
   namespaces, and the enforcement profile.
3. **Publish the plugin.** `plugin.py` exposes `PLUGIN` with
   `registration`, `prompts_dir`, and `corpus_dir` (`app/plugin.py`). The
   ledger runs none of your code; the plugin only publishes data.
4. **Publish the eval adapter.** `eval/adapter.py` exposes `ADAPTER` with
   `reasoner_id`, `event_type`, `datasets_dir`, and `build_driver(settings)`
   (`eval/adapter.py`). Declare both entry points, each named after your
   `reasoner_id`:

    ```toml
    [project.entry-points."decisionledger.reasoners"]
    <reasoner_id> = "<package>.plugin:PLUGIN"

    [project.entry-points."decisionledger.eval_adapters"]
    <reasoner_id> = "<package>.eval.adapter:ADAPTER"
    ```

5. **Assemble and submit.** Write `build_submission` (see
   [the assembler pattern](#the-assembler-pattern-ato-reference)) and a
   pipeline that submits through a `LedgerClient`.
   `LocalClient.from_settings()` composes the ledger in your process
   (plugins filtered by `DECISIONLEDGER_REASONERS`, the gates, the
   registration file) and writes to the same Postgres; `HttpClient` calls a
   ledger service. The reference reasoner runs as its own FastAPI service
   and picks between them at startup (`ATO_LEDGER_URL` set: HTTP).
6. **Ship gate assets.** Prompt templates go in `prompts/` (a top-level
   `system:` key carries the system message; template ids are global
   across a deployment; a change is a new file). Name your corpus on your
   binding to the RAG gate (`config: {corpora: [{corpus_id: <corpus_id>,
   tier: 1}]}`, DR-34) and load it with the gate's loader before the ledger
   starts:
   `uv run python -m policy_rag_gate.corpus_loader --corpus-id <corpus_id> --reasoner-id <reasoner_id>`
   (or `make build-policy-index-<reasoner_id>`, with
   `POLICY_CORPUS_ID_<reasoner_id> := <corpus_id>` in your `reasoner.mk`).
7. **Add `reasoner.mk`** (suffixed targets appended to
   `REASONER_INDEX_TARGETS`, `REASONER_PREPARE_TARGETS`,
   `REASONER_EVAL_TARGETS`, and the others; the root `Makefile` includes
   every `reasoners/*/reasoner.mk`) and, if you need infrastructure of your
   own, a `compose.yaml` added to the root `docker-compose.yml` `include:`
   list and a settings class with your own environment prefix.
8. **Install.** Add the distribution to the root `reasoners` dependency
   group with a `{ workspace = true }` source and run `uv sync`. Entry
   points are written at install time, so re-run `uv sync` after editing
   the member's `pyproject.toml`. If the reasoner has integration or eval
   jobs of its own, add its directory name to the CI matrices
   (`matrix.reasoner` in `.github/workflows/integration.yaml` and
   `eval.yaml`).
9. **Check.** Your member's tests run under the root `pytest` with no
   Docker, `tests/test_framework_boundary.py` must still pass, and the
   conformance suite checks the ledger's HTTP contract.

---

## New-gate checklist

A gate is a plugin the ledger runs on a decision. The contract in depth is
in [How to implement a gate](../gates/implementation.md); the steps:

1. **Create a workspace member** under `gates/<name>/` with its own
   distribution and import package, depending on `decision-ledger`. The
   reference members are `gates/rules` and `gates/policy_rag`.
2. **Publish a `GatePlugin`** (`app/gates.py`) under the
   `decisionledger.gates` entry-point group, named after its `gate_id`:
   `gate_id`, `kind` (`llm`, `lookup`, or `rules`), `deterministic` (Tiers 2
   and 4 are skipped for a deterministic gate), `config_model` (a Pydantic
   model that validates `gate_config` at intake, or `None`), optionally
   `binding_config_model` (a Pydantic model that validates a registration's
   `GateBinding.config` for your gate; DR-33), and
   `build(framework) -> Gate`.
3. **Implement the `Gate`.** `async evaluate(request: GateRequest) ->
   GateOutput` and `close()`. `evaluate` must not block the event loop:
   run blocking I/O or CPU-bound work in a thread and await it. Return a
   plain `GateOutput` with your `gate_id` and declared `deterministic`;
   put gate-specific data in `evidence_snapshot` and `extra`; key a
   decision about a person on `request.identity`, not on
   `submission.entity_id`.
4. **Expect to fail closed.** An exception, a timeout past
   `request.budget_ms`, a malformed or unstorable output, or
   `verdict=None` is recorded and holds the decision; plan your own
   timeouts within the budget.
5. **Add optional capabilities** when you need them: `ConfigAuthorizer`
   (`authorize_config(config, *, reasoner_id)`, called at intake) to allow
   a configuration per reasoner; `BindingValidator`
   (`validate_binding(reasoner_id, config)`, raising `GateConfigError`) to
   admit or refuse each registration's binding to your gate, asked at
   startup and when a registration is written over the API (a gate with
   neither it nor a `binding_config_model` refuses a binding that carries
   a configuration; admitting is not remembering: the framework hands the
   binding's `config` to every request as `GateRequest.binding_config`);
   `ReasonerScope` (`serves(reasoner_id)`, asked right after the
   binding is admitted) when the gate can serve only some reasoners; and
   `EvidenceVerifier` (`async verify(output, *, live)`, one
   `EvidenceStatus` per evidence reference, DR-34) to say, when a decision
   is verified, how the evidence your gate cited stands. To keep a copy of
   what it relied on, take the store's escrow in `build`
   (`framework.store.artifacts`), call its `put` from a thread while
   evaluating, and cite the digest it returns as `artifact_hash` (none with
   escrow off); see
   [Escrow and verify](../gates/implementation.md#escrow-and-verify).
6. **Own your infrastructure**: a `compose.yaml` the root
   `docker-compose.yml` includes, your own schema and DDL (never the
   `decisionledger` schema), a `gate.mk` with suffixed targets appended to
   the root `GATE_*_TARGETS` aggregates, and a settings class with your own
   prefix (`RULES_GATE_`, `POLICY_RAG_GATE_`). A gate with no infrastructure
   of its own, like the rules gate, ships only the settings class.
7. **Stay reasoner-agnostic.** Read only the request; a reasoner's specific
   assets reach you through its plugin (`framework.reasoners`), its
   binding's `config`, its `gate_config`, or, for a reasoner registered
   without a plugin, from where your gate documents (read it when you admit
   the binding, as the RAG gate reads its asset root).
8. **Test and evaluate it yourself**: the member's own tests, and its own
   eval harness for anything model-backed (no mocked LLM unit tests).
9. **Install** it through the root `gates` dependency group with a
   workspace source, then `uv sync`.

---

## Zero framework diff

A new reasoner is correct when it needs no edit under `core/`, `app/`, or
`eval/`. `tests/test_framework_boundary.py` checks what a machine can:

- No framework module imports a reasoner or gate package, or anything from
  `tests/`. No framework file names a workspace reasoner (its
  distribution, import package, or `reasoner_id`) or the reference
  reasoner's vocabulary (ATO's types, templates, features, jurisdictions,
  and rule ids).
- No framework file names the removed handoff identifiers or the old
  routing vocabulary, and the root `pyproject.toml` depends on exactly the
  framework set and declares no entry point; every reasoner and gate member
  declares its own.
- No framework file mounts a reasoner router or builds a reasoner's
  services.

The executable proof is the test doubles: `tests/_stub_reasoner` (a
second, minimal reasoner), `tests/_fake_gate`, and `tests/_stub_resolver`,
which the framework suite composes without any framework module knowing
they exist.

---

## Replay guarantee

Replay (`POST /api/v1/decisions/{decision_id}/replay`) needs no reasoner
code and invokes no gate and no model. From the bundle alone, and the
snapshot of the registration version it cites, it:

1. re-runs each gate's gate-level enforcement on the gate's stored output
   (a gate that failed fails closed again; a skipped gate stays skipped);
2. re-evaluates the snapshotted bypass rules, the cited one and every
   competing one, against the stored `reasoner_context`;
3. re-runs the decision-level rules and the combinator under the
   registration snapshot's enforcement profile;

and reports the original and replayed actions, whether they match, the
original and replayed `bypass_rule_holds`, each gate's result, and the
registration version. Enforcement is deterministic, so the same stored
outputs give the same action. The guarantee holds because each gate's
output, the LLM's included, is stored, not because a model is
deterministic. A bundle whose cited version has no snapshot and is not the
current one is 409 `REGISTRATION_SNAPSHOT_MISSING`; a schema-version-1
bundle is 409 `BUNDLE_SCHEMA_UNSUPPORTED`.

Verify (`POST /api/v1/decisions/{decision_id}/verify`, DR-34) is not
replay: it asks each gate that ran how the evidence it cited stands now
(against the copy it escrowed in the ledger, or with `?live=true` against
the source itself), beside the ledger's own check that its escrow still
holds that copy. Like replay it needs no reasoner code, and it changes
nothing.
