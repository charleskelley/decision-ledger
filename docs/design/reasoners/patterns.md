# Reference reasoner patterns

The repository carries one mock reasoner per valid reasoner pattern, so the
framework's contracts are demonstrated and tested in place, not only by the
Account Takeover reference reasoner
([DR-35](../decisions.md#dr-35-reference-reasoner-patterns-ship-as-workspace-members)).
A mock demonstrates contracts; it is not a product, and its domain is
imaginary. Each mock:

- is its own uv workspace member under `reasoners/`, with the example
  reasoner's shape: two entry points, a `reasoner.mk`, `tests/` without
  `__init__.py`, a README, and a reference page;
- ships a real `ReasonerPlugin`, which the root `reasoners` dependency group
  installs, so a default composition (`DECISIONLEDGER_REASONERS` blank)
  serves it and `make prepare-reasoners` loads its corpus, if it has one;
- runs on the shared infrastructure only (the framework's Postgres, and the
  infrastructure of the gates it binds), bringing none of its own;
- never enters `make eval`: its eval joins `make eval-mocks` (Postgres
  only, and no model call: the mock's own gate double, or a gate that calls
  none);
- is never imported by the ledger: no file under `core/`, `app/`, or
  `eval/` names it, and the boundary test enforces that for every reasoner
  member.

| Pattern | Member | On this page |
|---|---|---|
| Stream: a real-time event scorer | `reasoners/mock_stream` (`mock-stream-reasoner`) | [Stream](#stream-the-real-time-event-scorer) |
| Batch: a daily batch scorer | `reasoners/mock_batch` (`mock-batch-reasoner`) | [Batch](#batch-the-daily-batch-scorer) |
| Text: an LLM-native classifier | `reasoners/mock_text` (`mock-text-reasoner`) | [Text](#text-the-llm-native-classifier) |
| Daily: a deterministic computation with exceptions | `mock-daily-reasoner` | Recorded in DR-35 |

The patterns are recorded in DR-35; each section is added with its member.
The contracts the mocks exercise are the
[Submission contract](reasoner-handoff.md) and
[Registration](../registration.md).

---

## Stream: the real-time event scorer

`reasoners/mock_stream` (distribution and `reasoner_id`
`mock-stream-reasoner`, package `mock_stream_reasoner`; API reference:
[`mock_stream_reasoner`](../../reference/reasoners/mock-stream.md)). Imaginary
accounts send transfers on a stream; the mock scores each transfer against
the account's recent activity, records the clear cases, and asks the
ledger to gate the rest through the [policy gate](../gates/policy/index.md).

### What it proves

- **Stream semantics.** An async in-memory source stands in for a broker:
  a payload that is not an event is dead-lettered as `malformed`; a second
  delivery of an `event_id` is dropped and counted (dedup); an event older
  than the watermark by more than the allowed lateness (5 minutes) is
  dead-lettered as `late`. What the ledger refuses (a 4xx) is dead-lettered
  as `rejected`, a transport error or a 5xx as `unreachable`, anything else
  as `failed`, and the consumer never stops on one event.
- **Per-entity sliding state on event time, with a real reset.** Each
  transfer's features come only from its account's transfers timestamped
  before it: the 10-minute window, the baseline, and the history (so
  `thin_history`). Precursors that arrived in any order give it the same
  features; one that arrives after it (within the lateness bound) counts for
  the transfers scored after that, never for one already scored.
  `reset_state` (and the service's `POST /api/v1/stream/reset`) forgets the
  windows, the dedup set, and the watermark. The eval's consistency
  scenarios pin it: reordered precursors, the same decision.
- **Record bands and a gated middle (DR-30).** `activity_score` below 0.2
  is recorded ALLOW under `steady-activity`, above 0.9 recorded BLOCK under
  `activity-surge`; everything between asks the ledger to gate, with no
  proposal. `feature_set` is complete, so the bypass rules and replay need
  nothing else.
- **An advisory policy gate bound to the mock's own corpus (DR-29,
  DR-34).** The binding names `mock-stream-practices` (two practice notes)
  at tier 2, below regulation, with `min_provenance: pinned_version`; every
  submission, recorded ones too, carries the gate's context (template
  `mock-stream-v1`, which renders evidence by tier with the conflict/gap
  protocol).
- **Tier 3 on fresh accounts, with a real gate verdict.** The registration
  names `thin_history` as its `novel_entity_feature`. A fresh account's
  large first transfer scores between the bands, the gate returns its
  verdict, and Tier 3 holds the decision for review
  (`tier3_novel_entity`); a failed gate would hold it by Tier 1 instead.
- **A native subject (DR-32).** The account is `(mock-stream-account,
  account_id)` in the namespace the registration declares.
- **Its own service and CLI.** A FastAPI service (port 8002) whose consumer
  decides each accepted event over HTTP (`MOCK_STREAM_LEDGER_URL`,
  `MOCK_STREAM_LEDGER_KEY`; a blank URL refuses to start), and a CLI to
  serve, replay a synthetic stream into it, and print its registration
  file.
- **Three registration sources (DR-33).** By its plugin (the default), by
  a registration file (`reasoners/mock_stream/registration.yaml`, generated
  from `registry.py`), or by `PUT /api/v1/reasoners/mock-stream-reasoner`
  with the operator key, the last two with its plugin excluded; all three
  serve one `registration_version`.
- **An asset-root layout.** `src/mock_stream_reasoner/assets/` is laid out
  as the mock's directory under the policy gate's asset root
  (`prompts/mock-stream-v1.yaml`, `corpus/*.md`), so a ledger that does not
  compose the plugin serves the same template and corpus.

### Walk-through: a ledger that does not install the plugin

This checkout installs the mock's plugin, so the walk-through leaves it out
of the composition with `DECISIONLEDGER_REASONERS`, which is what a
deployment without the member looks like to the ledger. Prerequisites: the
stack is up (`docker compose up -d`), and `OPENAI_API_KEY` is set, since the
gated decisions call the policy gate's model (ten per default replay).

```bash
# 1. Load every corpus the default composition binds (ATO's, the example's,
#    and the mocks', from their plugins) and train missing models.
make prepare-reasoners

# 2. The mock's directory under an asset root
#    (outputs/stage/assets/mock-stream-reasoner/{prompts,corpus}).
make assets-mock_stream
export POLICY_RAG_GATE_ASSETS_ROOT=$(pwd)/outputs/stage/assets

# 3. Leave the plugin out. The ledger and the corpus loader read the same
#    allowlist, so export it for both.
export DECISIONLEDGER_REASONERS=ato-reasoner,example-reasoner

# 4. Load the mock's corpus from the asset root.
make build-policy-index-mock-stream-reasoner
# = uv run python -m policy_rag_gate.corpus_loader \
#     --corpus-id mock-stream-practices --reasoner-id mock-stream-reasoner
# Its corpus_loader.start log line names the directory it read:
#   corpus_dir <repo>/outputs/stage/assets/mock-stream-reasoner/corpus

# 5. Start the ledger with the mock's key and the operator key.
export DECISIONLEDGER_API_KEYS="example-reasoner:ex-key,ato-reasoner:ato-key,mock-stream-reasoner:ms-key"
export DECISIONLEDGER_OPERATOR_KEY=op-key
make serve
```

The loader reads a corpus from where the gate reads the reasoner's assets:
an explicit `--corpus-dir` wins; otherwise the `corpus_dir` of the composed
plugin whose `reasoner_id` matches (plugins filtered by
`DECISIONLEDGER_REASONERS`, as the ledger filters them), then
`<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus`. It asks no registry,
so load before you register: the gate describes every corpus a binding
names when it admits the binding, and refuses one that is not loaded.

In a second shell:

```bash
# 6. Register the mock over the API: the registration file's entry, as JSON.
uv run python -m mock_stream_reasoner registration-yaml \
  | uv run python -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)["registrations"][0]))' \
  > /tmp/mock-stream-registration.json
curl -s -X PUT localhost:8000/api/v1/reasoners/mock-stream-reasoner \
  -H 'X-DecisionLedger-Key: op-key' -H 'content-type: application/json' \
  --data @/tmp/mock-stream-registration.json
# Expected: 201, {"reasoner_id": "mock-stream-reasoner",
#   "registration_version": "20f6c04e5eb2cef6", "source": "api", ...}
curl -s localhost:8000/api/v1/reasoners/mock-stream-reasoner \
  -H 'X-DecisionLedger-Key: ms-key'

# 7. Start the mock's service (port 8002); it submits over HTTP.
MOCK_STREAM_LEDGER_URL=http://localhost:8000 MOCK_STREAM_LEDGER_KEY=ms-key \
  make serve-mock_stream
```

In a third shell:

```bash
# 8. Replay a synthetic stream of 200 events into the service.
make replay-mock_stream
# Expected report: outcomes accepted 195, duplicate 3, dead_lettered:late 1,
#   dead_lettered:malformed 1; modes record 185 (182 ALLOW under
#   steady-activity, 3 BLOCK under activity-surge) and gate 10;
#   decision_level_rules tier3_novel_entity 3 (the accounts whose ids
#   contain -fresh-); "gated" lists each gated decision_id.

# 9. Replay and verify a gated decision.
H='X-DecisionLedger-Key: ms-key'
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/replay -H "$H"
# Expected: "matched": true, under registration_version 20f6c04e5eb2cef6
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/verify -H "$H"
# Expected: the policy gate's references with "escrow": "present" and
#   "status": "intact"
```

Of the ten gated transfers, the three fresh accounts' are held by Tier 3
whatever the gate permits; the other seven (the middle of a burst, and
three established accounts' transfers six times their baseline) take the
gate's verdict. `GET /api/v1/stream/stats` on the service shows the
counters, the watermark, and whether the consumer has caught up; a second
`make replay-mock_stream` starts its stream just after the service's
watermark, so it is not dead-lettered as late.

### By file, and the one-source rule

A reasoner has one source. A ledger that composes the plugin or reads a
registration file naming `mock-stream-reasoner` while an API row for it
exists refuses to start, and a `PUT` for a reasoner the plugin or the file
registers answers `409 REGISTRATION_SOURCE_IMMUTABLE`. So delete the API row
before switching to the file (or back to the plugin); the snapshots stay,
so the bundles that cite them still replay:

```bash
# Stop the ledger, then:
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "delete from decisionledger.reasoner_registry where reasoner_id = 'mock-stream-reasoner'"

# Restart with the file, the plugin still excluded (same exports as above)
DECISIONLEDGER_REGISTRATIONS=reasoners/mock_stream/registration.yaml make serve

curl -s localhost:8000/api/v1/reasoners/mock-stream-reasoner -H 'X-DecisionLedger-Key: ms-key'
# Expected: "source": "file:reasoners/mock_stream/registration.yaml",
#   the same registration_version
```

The mock's service keeps running across the restart; replay another stream
into it and replay one of its gated decisions. To return to the default
composition, unset `DECISIONLEDGER_REASONERS` and
`DECISIONLEDGER_REGISTRATIONS` (and delete the API row, if one is left).

### Eval

`make eval-mocks` runs `make eval-mock_stream`: the framework eval harness
with the mock's adapter. Its datasets are consistency scenarios (a steady
account recorded ALLOW and a surge recorded BLOCK, each in two precursor
orderings) and robustness cases (a policy gate returning no verdict, and a
policy gate failing; both hold). The driver composes the ledger over
Postgres with the mock's plugin and a scripted `policy` double
(`mock_stream_reasoner.testing`: binding admission in the real gate's
shapes, a fixed CHALLENGE verdict), so it needs Postgres but no
Elasticsearch, corpus, or model call. The driver composes the plugin, so
run it with no API row for the mock and without
`DECISIONLEDGER_REGISTRATIONS` naming its file. The mock ships no
retrieval, faithfulness, citation, or authority datasets, so the policy
gate's own eval never selects it.

### Tests

The member's tests run without Docker over the in-memory ledger
(`tests._memory_ledger`) and the scripted policy double: the stream, the
state, the scorer, the assembler, the service, the generated registration
file against `registry.py` (one `registration_version`), and acceptance
tests that register the mock by file and by `PUT` with its plugin
excluded. A test checks the shipped assets against the real policy gate's
loader and models when that gate is installed. One
`@pytest.mark.integration` acceptance test runs over the live stack
(`make test-integration`, after `make prepare-reasoners`): it points
`POLICY_RAG_GATE_ASSETS_ROOT` at a temporary copy of the assets, registers
the mock by `PUT`, and deletes its registry row in teardown.

The member's README (`reasoners/mock_stream/README.md`) lists its modules,
service routes, CLI flags, make targets, and settings.

---

## Batch: the daily batch scorer

`reasoners/mock_batch` (distribution and `reasoner_id`
`mock-batch-reasoner`, package `mock_batch_reasoner`; API reference:
[`mock_batch_reasoner`](../../reference/reasoners/mock-batch.md)). An
overnight extract carries each imaginary account's end-of-day position; the
mock scores the day's rows once, records the rows its bypass rules cover in
batch requests, and submits the rest, its exceptions, one at a time for the
[rules gate](../gates/index.md#the-reference-gates) to decide.

### What it proves

- **A record-only batch (DR-30, DR-31).** The day's records go to
  `POST /api/v1/decisions:batch` as NDJSON, in row order, in chunks of at
  most 10,000 lines (the most the ledger takes in one request), so 25,000
  rows are three requests. The ledger answers line by line (`recorded`,
  with the decision and its action, or `rejected`, with a problem code),
  and the mock keeps every line's result. A record runs only the mandatory
  gates, and the mock binds its one gate advisory, so its records run none.
- **Four disjoint bypass rules with a deliberate gap (DR-30).** One rule
  per recordable action over the `delinquency_score` bands of an unflagged
  row: `current-account` (below 0.3, ALLOW), `watch-list` (0.3 to below
  0.6, CHALLENGE), `delinquent-hold` (0.6 to below 0.85, HOLD), and
  `charge-off` (from 0.85, BLOCK). Every rule requires `needs_review` to be
  false, a flag set upstream that the score never reads, so a flagged row
  matches no rule and is an exception. All four actions are recordable, so
  HOLD is recorded too, without the gate.
- **Exceptions through gate-mode submit and the rules gate (DR-28,
  DR-29).** Each exception goes to `POST /api/v1/decisions` on its own, in
  gate mode with no proposal, and the rules gate, bound advisory with an
  empty binding configuration, decides it under the rule set its context
  names. Every submission, records included, carries
  `GateContext(gate_id="rules", gate_config={"ruleset_id": "mock-batch-v1"})`,
  so a recorded row can also be re-decided offline by the shadow dimension.
  The mock reads each verdict back from its bundle: the rules that matched,
  whether the rule set's default applied, and any gate failure.
- **`BATCH_RECORD_ONLY`, provoked on purpose.** A batch is record-only: the
  ledger rejects a gate-mode line on its own, with the code
  `BATCH_RECORD_ONLY` and the line's `event_id`, and records the rest of
  the request. Each run sends its first exception in a batch request, at its
  row's place among the records; once the ledger has rejected that line, the
  mock submits the row like every other exception, so it is still decided.
  `run --no-provoke` turns this off, and the eval driver never provokes.
- **A cited rule that does not hold is a recorded HOLD, not a rejection.**
  The ledger stores the line with `bypass_rule_holds: false` under a HOLD
  floor (`bypass_rule_not_satisfied`), and the floor never relaxes, so a
  BLOCK record stays BLOCK. Only a static fault in the citation (an
  unregistered rule, a wrong action or version, an unrecordable action)
  rejects a record line. The mock cites only rules that hold under its own
  registration; `run` first checks that the ledger serves the same
  `registration_version` (and sends nothing otherwise), and its report
  counts any line recorded under the floor (`batch.cited_rule_not_held`).
- **A rule set as deployment configuration (DR-28).**
  `rulesets/mock-batch-v1.yaml` names `reasoner_id: mock-batch-reasoner`
  (required: the gate fails closed on a submission from any other reasoner
  that names the rule set). The rules gate reads one directory,
  `RULES_GATE_RULESETS_DIR`, when the ledger process builds it, so `make
  prepare-rulesets` stages every member's `rulesets/` in
  `outputs/stage/rulesets`, and the ledger is pointed there before it
  starts. The rules read only features every row carries (one predicate
  that cannot evaluate fails the whole evaluation) and are not conditioned
  on `needs_review`, so the shadow dimension re-decides recorded rows under
  the same rules. Every verdict cites the rule set, and the gate escrows a
  copy of it (DR-34), so an exception's verify reads it back.
- **No deduplication in the ledger.** Every line the ledger records is a
  new decision, whatever its `event_id`: the mock refuses a day that repeats
  an id, a random run tag keeps runs apart, and a day sent twice is
  recorded twice.
- **A native subject (DR-32).** The account is `(mock-batch-account,
  account_id)` in the namespace the registration declares, so an account is
  the same ledger subject every day.
- **No service, and no asset root.** A CLI scores a synthetic day and
  submits it over HTTP (`MOCK_BATCH_LEDGER_URL`, `MOCK_BATCH_LEDGER_KEY`; a
  blank URL refuses to run), prints a JSON report, and prints the
  registration file. The plugin's `prompts_dir` and `corpus_dir` are empty:
  the mock binds no gate that reads them.
- **Three registration sources (DR-33).** By its plugin (the default), by
  a registration file (`reasoners/mock_batch/registration.yaml`, generated
  from `registry.py`), or by `PUT /api/v1/reasoners/mock-batch-reasoner`
  with the operator key, the last two with its plugin excluded; all three
  serve one `registration_version`.

### Walk-through: a ledger that does not install the batch plugin

As in the stream walk-through, the checkout installs the plugin, so
`DECISIONLEDGER_REASONERS` leaves it out of the composition. Prerequisites:
the stack is up (`docker compose up -d`), and no API row for the stream mock
is left from its walk-through, since this composition includes its plugin.
The batch mock's decisions call no model: its exceptions go to the rules
gate. The ledger still builds the RAG policy gate for the other reasoners,
and that gate's model client needs `OPENAI_API_KEY` (in the environment or
`.env`) when it is built.

```bash
# 1. Load every corpus the composition below binds (ATO's, the example's,
#    and the stream mock's; the batch mock binds none) and train missing
#    models.
make prepare-reasoners

# 2. Stage the members' rule sets (today the batch mock's alone) and point
#    the ledger at them: the rules gate reads the directory when the ledger
#    starts.
make prepare-rulesets
# Expected: prepare-rulesets: staged in outputs/stage/rulesets:
#   mock-batch-v1.yaml
export RULES_GATE_RULESETS_DIR=$(pwd)/outputs/stage/rulesets

# 3. Leave the batch plugin out; keep ATO's, the example's, and the stream
#    mock's.
export DECISIONLEDGER_REASONERS=ato-reasoner,example-reasoner,mock-stream-reasoner

# 4. Start the ledger with the mock's key and the operator key.
export DECISIONLEDGER_API_KEYS="example-reasoner:ex-key,ato-reasoner:ato-key,mock-stream-reasoner:ms-key,mock-batch-reasoner:mb-key"
export DECISIONLEDGER_OPERATOR_KEY=op-key
make serve
# Its rules_gate.built log line names the rule set:
#   ruleset_ids ['mock-batch-v1']
```

In a second shell:

```bash
# 5. Register the mock over the API: the registration file's entry, as JSON.
uv run python -m mock_batch_reasoner registration-yaml \
  | uv run python -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)["registrations"][0]))' \
  > /tmp/mock-batch-registration.json
curl -s -X PUT localhost:8000/api/v1/reasoners/mock-batch-reasoner \
  -H 'X-DecisionLedger-Key: op-key' -H 'content-type: application/json' \
  --data @/tmp/mock-batch-registration.json
# Expected: 201, {"reasoner_id": "mock-batch-reasoner",
#   "registration_version": "f81f57ceb103eb8a", "source": "api", ...}

# 6. Score a synthetic day of 25,000 accounts and submit it.
MOCK_BATCH_LEDGER_URL=http://localhost:8000 MOCK_BATCH_LEDGER_KEY=mb-key \
  make run-mock_batch COUNT=25000
# Expected report: rows 25000, decided 25000, undecided 0, stopped null;
#   batch: three requests (10,000, 10,000, and 4,759 lines), recorded 24758
#   (ALLOW 20258, CHALLENGE 2531, HOLD 1241, BLOCK 728), cited_rule_not_held
#   0, rejected 1, rejected_codes {"BATCH_RECORD_ONLY": 1}: line 5 of
#   request 1, row-<run_tag>-000005, "provoked": true; exceptions: decided
#   242 (ALLOW 42, CHALLENGE 70, HOLD 62, BLOCK 68), refused 0, failures 0,
#   the matched rules by rule id, and a "sample" of five exceptions with
#   their decision_ids.

# 7. Replay and verify an exception.
H='X-DecisionLedger-Key: mb-key'
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/replay -H "$H"
# Expected: "matched": true, under registration_version f81f57ceb103eb8a
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/verify -H "$H"
# Expected: one rules-gate reference per matched rule ("source_id":
#   "mock-batch-v1@1", "locator": the rule id, or "default_action" when
#   none matched), each with "escrow": "present" and "status": "intact"
```

The ledger rejected one line of the first request, the provoked exception,
and the mock then decided that row through `submit` with the other 241, so
every row is decided. The ledger does not deduplicate: a second run of the
target records the day again (two decisions per account, under two run
tags), and so would a rerun with the same `--run-tag` (two decisions per
`event_id`). A ledger started without `RULES_GATE_RULESETS_DIR` serves no
rule set, so the rules gate fails every exception closed (`unknown_ruleset`)
and each one is held; the report counts them under `exceptions.failures`.

### The one-source rule, by file, and the eval

A reasoner has one source, and the mock's eval driver composes its plugin
against the same Postgres, so delete the API row before `make eval-mocks`
(DR-33 refuses one reasoner from two sources; the snapshots stay, so the
bundles that cite them still replay):

```bash
# Stop the ledger, then:
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "delete from decisionledger.reasoner_registry where reasoner_id = 'mock-batch-reasoner'"
make eval-mocks
```

The registration file works the same way, with the plugin still excluded
and no API row: restart the ledger with
`DECISIONLEDGER_REGISTRATIONS=reasoners/mock_batch/registration.yaml`, and
`GET /api/v1/reasoners/mock-batch-reasoner` answers
`"source": "file:reasoners/mock_batch/registration.yaml"` with the same
`registration_version`. The eval driver reads that setting too, so unset it
before `make eval-mocks`. To return to the default composition, unset
`DECISIONLEDGER_REASONERS` and `DECISIONLEDGER_REGISTRATIONS`: the plugin
serves the same registration, and a run needs only the mock's key and the
staged rule set.

### Eval

`make eval-mocks` runs `make eval-mock_batch` beside the stream mock's
eval: the framework eval harness with the batch mock's adapter. Its
datasets are consistency scenarios (a current account recorded ALLOW, and
a flagged account the rules gate holds, each in two orderings of one day)
and robustness cases (a rules gate returning no verdict, and a rules gate
failing; all four hold). The driver composes the ledger over Postgres with
the mock's plugin and the real rules gate over the member's own
`rulesets/` (`RulesGatePlugin(RulesGateSettings(rulesets_dir=...))`), and
runs each ordering as the CLI runs a day, without the provocation, so it
needs Postgres but no rule-set staging, Elasticsearch, or model call. The
harness's shadow dimension re-decides a sample of the run's recorded rows
with the rules gate (report-only). The driver composes the plugin, so run
it with no API row for the mock and without `DECISIONLEDGER_REGISTRATIONS`
naming its file.

### Tests

The member's tests run without Docker over the in-memory ledger
(`tests._memory_ledger`) and the real rules gate, built over the member's
rule set: the rows, the scorer, the assembler, the synthetic day, the plan
and the run (the provoked `BATCH_RECORD_ONLY` line, asserted on its `code`
and `event_id`; refusals; a cited rule that does not hold; a ledger
without the rule set; replay and verify), the rule set against the
plugin's `reasoner_id`, the CLI, the eval driver over its datasets, and the
generated registration file against `registry.py` (one
`registration_version`). One `@pytest.mark.smoke` test runs a synthetic day
end to end, also without Docker.

The member's README (`reasoners/mock_batch/README.md`) lists its modules,
the rule set, the CLI's flags, report, and exit status, the default run's
counts, make targets, and settings.

---

## Text: the LLM-native classifier

`reasoners/mock_text` (distribution and `reasoner_id`
`mock-text-reasoner`, package `mock_text_reasoner`; API reference:
[`mock_text_reasoner`](../../reference/reasoners/mock-text.md)). Customers
write short messages to an imaginary bank's automated assistant; the mock
labels each message with a deterministic keyword classifier, standing in
for a model, and asks the ledger to gate every one through the
[policy gate](../gates/policy/index.md), bound to two corpora of different
authority that disagree on one category.

### What it proves

- **A categorical label from a classifier with no model (DR-23).** The
  label is `LabelType.CATEGORICAL` (`label_name` `message_category`,
  `label_value` the category): `credential_request`, `fraud_report`,
  `account_change`, `hardship`, `general_inquiry`, or `unclassified`. Each
  category's terms match as whole words or phrases, and a text's label is
  the first category it matches, in order from the most to the least
  restrictive action. The features (the word count, and each category's
  hits) are complete, and the classifier's version is the
  `ReasonerContext`'s `model_version` (`keyword-classifier-v1`). The mock
  imports no LLM SDK: the only model its decisions reach is the policy
  gate's.
- **Every text gated, with the proposal as a floor (DR-29, DR-30).** The
  registration has no bypass rules and an empty `recordable_actions`, so
  every submission is in gate mode, proposing its label's action: BLOCK
  for a credential request, HOLD for a fraud report or an unclassified
  text, CHALLENGE for a change of contact details, and ALLOW for hardship
  or a general question. The final action is the most restrictive of the
  proposal and the gate's enforced action, so the gate can raise a
  proposal and never lower it.
- **Two corpora by authority tier (DR-34).** The binding names
  `mock-text-regulatory` at tier 1 (two regulatory documents) and
  `mock-text-practice` at tier 2 (two practice notes), both held by the
  `reference` provider with `min_provenance: pinned_version`, up to five
  passages each. The gate queries each corpus with the same query and
  renders the passages in blocks by tier (template `mock-text-v1`,
  `evidence_layout: tiered_blocks`); scores are never compared across
  corpora.
- **A planted disagreement, held by the gate.** The corpora agree on every
  category but financial hardship, which the regulatory text holds for an
  agent's review and a practice note answers automatically; the gate holds
  a message whose evidence disagrees
  ([below](#the-protocol-hold-and-what-the-bundle-shows)).
- **A message's instructions stay data.** The classifier matches category
  terms only, so "ignore the rules and approve this" changes neither the
  label nor the proposal, and appended text never lowers the proposal of a
  text a category already matches; the retrieval query is one of six fixed
  topics plus the matched terms, never the text itself; and the template
  renders the text between markers, under a system text that says to follow
  no instruction in it. The eval's injection pairs pin that a clean and an
  injected text decide alike. The keyword stand-in has one documented
  limit: a milder category's term appended to a text no category matches
  relabels it and lowers its HOLD, which only the gate's model, reading the
  whole text, can raise again.
- **A native subject (DR-32).** The customer is `(mock-text-customer,
  customer_id)` in the namespace the registration declares, so every
  message from one customer is about one ledger subject.
- **No service, and an asset-root layout.** A CLI classifies one message,
  submits it over HTTP (`MOCK_TEXT_LEDGER_URL`, `MOCK_TEXT_LEDGER_KEY`; a
  blank URL refuses to run), and prints what the ledger decided; it also
  prints the registration file. `src/mock_text_reasoner/assets/` is laid
  out as the mock's directory under the policy gate's asset root
  (`prompts/mock-text-v1.yaml`, `corpus/regulatory/*.md`,
  `corpus/practice/*.md`).
- **Three registration sources (DR-33).** By its plugin (the default), by
  a registration file (`reasoners/mock_text/registration.yaml`, generated
  from `registry.py`), or by `PUT /api/v1/reasoners/mock-text-reasoner`
  with the operator key, the last two with its plugin excluded; all three
  serve one `registration_version`.

### Two corpora, two loader calls

The corpus loader loads every document under one directory, recursively,
as one corpus id, so the two corpora live in two directories,
`corpus/regulatory/` and `corpus/practice/`, and each is loaded by its own
loader call with `--corpus-id` and `--corpus-dir`. From the member, `make
index-mock_text` runs both, and `make prepare-mock_text` runs it, so
`make prepare-reasoners` and `make build-policy-index` load both corpora
with every other reasoner's:

```bash
# make index-mock_text runs:
uv run python -m policy_rag_gate.corpus_loader --corpus-id mock-text-regulatory \
  --reasoner-id mock-text-reasoner \
  --corpus-dir reasoners/mock_text/src/mock_text_reasoner/assets/corpus/regulatory
uv run python -m policy_rag_gate.corpus_loader --corpus-id mock-text-practice \
  --reasoner-id mock-text-reasoner \
  --corpus-dir reasoners/mock_text/src/mock_text_reasoner/assets/corpus/practice
```

A deployment that registers the mock without its plugin loads the same
two directories from the mock's copy under the asset root (`make
assets-mock_text`): `--corpus-dir <root>/mock-text-reasoner/corpus/regulatory`
and `--corpus-dir <root>/mock-text-reasoner/corpus/practice`, as the
[local runbook](../../development/runbook-local.md#a-reasoner-without-a-plugin)
shows. The member's `reasoner.mk` has no
`POLICY_CORPUS_ID_mock-text-reasoner` line: the policy gate's
`build-policy-index-<reasoner_id>` target loads a reasoner's whole corpus
directory as the one corpus that variable names, which here would merge
both subtrees under one id, so for this mock the target stops and asks for
the variable instead. The plugin's `corpus_dir` is the parent of the two
directories, and the gate reads it recursively for the jurisdictions the
mock may filter to, which covers both. Load both corpora before the ledger
starts (or before the registration is written): the gate describes each
corpus a binding names when it admits the binding, and refuses one that is
not loaded.

### The protocol hold, and what the bundle shows

`MT-REG-ASSISTANT` section 4 (`mock-text-regulatory`, tier 1) requires a
trained agent's review before any reply to a statement of financial
hardship, the automated hardship-assistance reply included;
`MT-PRAC-REPLIES` section 4 (`mock-text-practice`, tier 2) permits that
automated reply at once, without review. On every other category the two
corpora agree. Template `mock-text-v1` asks the model for the report of the
[conflict/gap protocol](../gates/policy/evidence.md#the-conflictgap-protocol),
and to report a disagreement across tiers even though the higher tier
governs: a model that applies precedence silently would permit HOLD
without saying that the evidence disagreed.

When the model reports a disagreement (`conflict_detected`), the gate
floors its verdict at HOLD and escalates with its own constant reason,
"Retrieved policy evidence on point disagrees; held for human review."
Neither "conflict" nor "jurisdiction" is in it, so enforcement's
jurisdiction-conflict tier (Tier 5) does not claim the hold: enforcement
accepts the gate's verdict (or, below 0.60 confidence, credits its
low-confidence tier, with the same HOLD). The `hardship` label proposes
ALLOW, so the HOLD is the gate's, not the proposal's. The bundle's
`policy` gate record shows:

- `output.verdict`: `permitted_actions` floored at HOLD,
  `escalate_to_human: true`, and the gate's reason;
- `output.extra`: `conflict_detected: true`, `protocol_hold:
  "conflict_detected"`, the `governing_corpus_id` the model named,
  `evidence_sufficient`, and `verdict_as_returned`, the model's verdict
  before the floor;
- `output.evidence_snapshot`: two `CorpusQuery` entries, one per bound
  corpus (`mock-text-regulatory` at tier 1, then `mock-text-practice` at
  tier 2), and the rendered prompt, its passages in two tier blocks;
- `output.evidence`: one reference per rendered passage, from documents of
  both corpora.

Replay re-enforces the stored output and calls no model; verify checks
each reference against the document the gate escrowed for it. Whether a
model notices the disagreement is shown only live: the mock's scripted
double can answer chosen labels with the hold's shape, for its tests, and
the eval composes it without that.

### Walk-through: the default composition

The default composition serves the text mock through its plugin, so the
walk-through needs no exclusion. Prerequisites: the stack is up (`docker
compose up -d`); `OPENAI_API_KEY` is set (in the environment or `.env`),
since every message the mock submits is one call to the policy gate's
model; no API row is left for any mock from the walk-throughs above, since
this composition includes every plugin; and neither
`DECISIONLEDGER_REASONERS` nor `DECISIONLEDGER_REGISTRATIONS` is set (the
batch walk-through exports the first: `unset DECISIONLEDGER_REASONERS
DECISIONLEDGER_REGISTRATIONS`), so the text plugin is in the
composition.

```bash
# 1. Load every corpus the default composition binds, the text mock's two
#    among them, and train missing models.
make prepare-reasoners
# Expected, among its output, one corpus_loader.complete line per corpus,
#   these two among them: corpus_id=mock-text-regulatory (documents=2,
#   chunks=6) and corpus_id=mock-text-practice (documents=2, chunks=7)

# 2. Start the ledger with the mock's key and the operator key (the staged
#    rule set is the batch mock's: this composition serves it too).
make prepare-rulesets
export RULES_GATE_RULESETS_DIR=$(pwd)/outputs/stage/rulesets
export DECISIONLEDGER_API_KEYS="example-reasoner:ex-key,ato-reasoner:ato-key,mock-stream-reasoner:ms-key,mock-batch-reasoner:mb-key,mock-text-reasoner:mt-key"
export DECISIONLEDGER_OPERATOR_KEY=op-key
make serve
```

In a second shell:

```bash
H='X-DecisionLedger-Key: mt-key'

# 3. The registration the ledger serves: the two-corpus binding.
curl -s localhost:8000/api/v1/reasoners/mock-text-reasoner -H "$H"
# Expected: "source": "plugin:mock_text_reasoner.plugin",
#   "registration_version": "4367bd37419a3334", and the policy binding's
#   config.corpora: mock-text-regulatory at tier 1, mock-text-practice at
#   tier 2

# 4. Decide the planted text: a statement of financial hardship.
export MOCK_TEXT_LEDGER_URL=http://localhost:8000 MOCK_TEXT_LEDGER_KEY=mt-key
uv run python -m mock_text_reasoner decide \
  --text "I lost my job and can't pay this month's installment."
# Expected report: "label": "hardship", "proposed_action": "ALLOW",
#   "decision_action": "HOLD"; under "policy": "status": "ran",
#   "escalate_to_human": true, "conflict_detected": true,
#   "protocol_hold": "conflict_detected", and "evidence_sources" naming
#   documents of both corpora (MT-REG-... and MT-PRAC-...).
# A model judges the evidence: when it does not report the disagreement
#   ("conflict_detected": false), decide the text again.

# 5. Replay and verify it, and read its corpus queries from the bundle.
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/replay -H "$H"
# Expected: "matched": true, under registration_version 4367bd37419a3334
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/verify -H "$H"
# Expected: the policy gate's references, to documents of both corpora,
#   each with "escrow": "present" and "status": "intact"
curl -s localhost:8000/api/v1/decisions/<decision_id> -H "$H" | python3 -c '
import json, sys
out = json.load(sys.stdin)["gates"][0]["output"]
queries = out["evidence_snapshot"]["corpus_queries"]
print([(q["corpus_ref"]["corpus_id"], q["corpus_ref"]["tier"]) for q in queries])
print(out["extra"]["protocol_hold"])'
# Expected: [('mock-text-regulatory', 1), ('mock-text-practice', 2)], then
#   conflict_detected

# 6. Decide a text the corpora agree on: a general question.
uv run python -m mock_text_reasoner decide --text "What are your opening hours on Saturday?"
# Expected report: "label": "general_inquiry", "proposed_action": "ALLOW",
#   and under "policy" no protocol hold ("protocol_hold": null): both
#   corpora permit an automated answer, so the gated path is not always HOLD.
```

To register the mock without its plugin instead, leave it out of
`DECISIONLEDGER_REASONERS`, copy its assets under the asset root, load both
corpora from the copy (above), and register it by file or by `PUT`, as the
[local runbook](../../development/runbook-local.md#a-reasoner-without-a-plugin)
shows; delete the API row afterward, before `make eval-mocks` or a return
to the default composition (DR-33 refuses one reasoner from two sources;
the snapshots stay, so the bundles that cite them still replay).

### Eval

`make eval-mocks` runs `make eval-mock_text` beside the stream and batch
mocks' evals: the framework eval harness with the text mock's adapter. Its
datasets are consistency scenarios (a general question decided ALLOW, and
a change of address decided CHALLENGE, each after two earlier messages
from the same customer, in two orderings) and robustness cases: three
injection pairs (the injected text appends instructions to the reviewer,
and each pair decides alike), three novel texts the classifier cannot
place (unclassified, so the proposal holds them), and a policy gate
returning no verdict and a policy gate failing (two cases each; all hold).
The driver composes the ledger over Postgres with the mock's plugin and
its scripted `policy` double (`mock_text_reasoner.testing`: binding
admission in the real gate's shapes, two corpora included, and a verdict
that permits the submission's proposal), so it needs Postgres but no
Elasticsearch, corpus, or model call; the report-only shadow dimension
finds no recorded decision to re-decide, since the mock records nothing.
The driver composes the plugin, so run it with no API row for the mock and
without `DECISIONLEDGER_REGISTRATIONS` naming its file. The mock ships no
retrieval, faithfulness, citation, or authority datasets, so the policy
gate's own eval never selects it: its two-corpus path is shown live, as
above.

### Tests

The member's tests run without Docker over the in-memory ledger
(`tests._memory_ledger`) and the scripted policy double: the message, the
classifier, the assembler, the pipeline (the proposal as a floor, a gate
that raises held by Tier 1, replay, and, with a double that holds a
hardship message as the real gate does, the protocol hold's shape in a
bundle), the CLI, the plugin and its make include, the eval driver over its
datasets (the framework's own dimensions included), and the generated
registration file against `registry.py` (one `registration_version`). When
the real policy gate is installed, tests run its loader and models over
the shipped assets: a copy loads from an asset root (`load_root_assets`),
every document of both corpora parses with complete front matter and is
cut into one chunk per section, the planted pair is on point for the
hardship label and differs in text, and every gate context the mock sends
validates and is authorized (`PolicyGateConfig`). No test calls a model or
needs Postgres.

The member's README (`reasoners/mock_text/README.md`) lists its modules,
the categories, the corpora, the CLI's flags, report, and exit status, make
targets, and settings.
