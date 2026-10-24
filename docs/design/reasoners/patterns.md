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
| Text: an LLM-native classifier | `mock-text-reasoner` | Recorded in DR-35 |
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
#    and the mock's, from its plugin) and train missing models.
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

# 3. Leave the batch plugin out; keep the others.
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
