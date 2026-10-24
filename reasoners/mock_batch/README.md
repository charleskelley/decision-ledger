# mock-batch-reasoner

The daily batch scorer pattern for DecisionLedger, packaged as a uv
workspace member. Import package: `mock_batch_reasoner`; distribution:
`mock-batch-reasoner`; `reasoner_id`: `mock-batch-reasoner`.

It is one of the reference reasoner patterns (DR-35): the repository carries
one mock reasoner per valid reasoner pattern, so the framework's contracts
are demonstrated and tested in place. A mock demonstrates contracts; it is
not a product. The domain is imaginary: an overnight extract carries each
account's end-of-day position, and a transparent heuristic scores how far
behind the account is. The mock scores a day once, records every row a
bypass rule covers through the ledger's batch endpoint, and submits its
exceptions, the rows flagged `needs_review` that no rule covers, one at a
time in gate mode, where the rules gate decides them. It has no service and
no infrastructure of its own: its CLI reaches the ledger over HTTP, and the
ledger never imports it, whichever way it is registered (its plugin, a
registration file, or a `PUT`). The design page is
`docs/design/reasoners/patterns.md`.

| Module | What it shows |
| --- | --- |
| `rows.py` | The mock's own row (`AccountDay`: `event_id`, an aware `as_of`, `account_id`, `balance` ≥ 0, `credit_limit` > 0, `days_past_due` and `returned_payments` ≥ 0, and `needs_review`; strict, unknown fields refused, every value one the ledger can store). It names the account as its native subject, `(mock-batch-account, account_id)`, and derives `entity_id` (a UUID5 of `account_id`, type `account`) under that declared namespace, so every day's row for an account is about one ledger subject. |
| `scorer.py` | A pure scorer: `delinquency_score = logistic(-3.2 + 2.2·min(utilization, 1.5) + 0.035·days_past_due + 0.8·returned_payments)`, with each term's contribution and every feature it read. It never reads `needs_review`, and it reads one row alone, so the order of a day's rows never changes a score. |
| `registry.py` | The registration: the rules gate (advisory, no binding configuration), four bypass rules over the score bands of an unflagged row (`current-account`, `watch-list`, `delinquent-hold`, `charge-off`), all four actions recordable, and the `mock-batch-account` native namespace. `RULESET_ID` is the rule set every submission names; `registration_yaml()` writes the registration file. |
| `assembler.py` | The handoff: records a row under the bypass rule that holds (its action, citing the rule pinned to its version), or submits it in gate mode with no proposal, as an exception. Every submission, records included, carries the rules gate's context, `{"ruleset_id": "mock-batch-v1"}`. |
| `batch.py` | `score_rows` (it refuses a day that repeats an `event_id`), `plan_day` (the batch requests' lines, in row order, and the exceptions), and `run_day`, which sends the lines, submits the exceptions, reads each exception's verdict back from its bundle, and returns the run (`DayRun`: the report, and every line's result). |
| `synthetic.py` | `synthetic_day`: a deterministic day for a seed, one row per account (`acct-000001` onward, the same accounts every day), with row ids under the run tag (`row-<run_tag>-<n>`). |
| `plugin.py` | What a ledger deployment that installs the member discovers: the registration, and empty `prompts/` and `corpus/` directories. `RULESETS_DIR` is the member's `rulesets/` in the workspace checkout. |
| `settings.py` | `MockBatchSettings` (`MOCK_BATCH_` prefix): the ledger URL (required) and key. |
| `eval/` | The `EvalAdapter`, an in-process driver over the real rules gate, and its datasets (consistency; robustness schema failure and fallback). |
| `__main__.py` | The CLI: `run`, `registration-yaml`. |

`registration.yaml` at the member root is the registration file, generated
from `registry.py` (`make registration-mock_batch`); do not edit it by
hand: a test holds the two to one `registration_version`.
`rulesets/mock-batch-v1.yaml`, also at the member root, is the rule set the
rules gate decides the exceptions under ([below](#the-rule-set)).

## How a day is decided

`run_day` scores every row and assembles its submission, then:

1. **Records, in batches.** Every row a bypass rule covers is a `record`
   submission. The mock sends them to `POST /api/v1/decisions:batch` as
   NDJSON, in row order, in chunks of at most 10,000 lines (the most the
   ledger takes in one request), and keeps every line's result: `recorded`,
   with its `decision_id` and action, or `rejected`, with a problem code. A
   record runs only the mandatory gates; the mock binds the rules gate
   advisory, so its records run no gate.
2. **Exceptions, one at a time.** Every row no rule covers (one flagged
   `needs_review`) is a `gate` submission with no proposal. The mock sends
   each to `POST /api/v1/decisions` on its own; the advisory rules gate
   decides it under `mock-batch-v1`, and the mock reads the verdict back
   from the decision's bundle (`GET /api/v1/decisions/{decision_id}`): the
   rules that matched, whether the rule set's default applied, and any gate
   failure.

What the ledger does with a line, and what the mock does about it:

- **`BATCH_RECORD_ONLY`, provoked on purpose.** A batch is record-only: the
  ledger rejects a gate-mode line on its own (`BATCH_RECORD_ONLY`, with the
  line's `event_id`) and records the rest of the request. Each run provokes
  that once: its first exception rides in a batch request at its row's
  place among the records (line 5 of the first request, in a synthetic
  day). Once the ledger has rejected that line, the mock submits the row
  in gate mode like every other exception, so the row is still decided.
  `run --no-provoke` turns the provocation off; the eval driver never
  provokes.
- **A cited rule that does not hold is a recorded HOLD, not a rejection.**
  The ledger stores the line with `bypass_rule_holds: false` under a HOLD
  floor (decision-level rule `bypass_rule_not_satisfied`); the floor never
  relaxes, so a BLOCK record stays BLOCK. The mock cites only a rule that
  holds under its own registration; a ledger serving another version of it
  could disagree, so `run` first checks that the ledger serves the same
  `registration_version` and sends nothing otherwise, and the report counts
  any line recorded this way (`batch.cited_rule_not_held`).
- **No deduplication.** The ledger records every line as a new decision,
  whatever its `event_id`, so a day must not repeat one (`score_rows`
  refuses it), and a day sent twice is recorded twice. The CLI's run tag,
  random by default, keeps runs apart.
- **Failures.** A refused or failed batch request (a missing or wrong key,
  for example), a failed read of a decided exception, or a ledger that
  cannot be reached stops the run; `run_day` raises `DayRunStopped`, which
  carries everything the ledger answered until then, and the CLI still
  prints the report (with `stopped`, the error) and writes `--results-out`.
  A line the ledger rejects for any other reason, or an exception it
  refuses, is reported, not retried, and the run goes on.

## Registration

| Bypass rule | Holds when (`label_value` is the `delinquency_score`) | Records |
| --- | --- | --- |
| `current-account` | below 0.3, and `needs_review` is false | ALLOW |
| `watch-list` | from 0.3 to below 0.6, and `needs_review` is false | CHALLENGE (contact the holder) |
| `delinquent-hold` | from 0.6 to below 0.85, and `needs_review` is false | HOLD (new spending held for review) |
| `charge-off` | from 0.85, and `needs_review` is false | BLOCK |

The bands are disjoint and cover every score, one rule per recordable
action; all four actions are recordable, so HOLD is recorded too, without
the gate. The gap is deliberate: `needs_review` is set upstream (an open
dispute, a manual flag), the score never reads it, and no rule covers a
flagged row, so that row is an exception. Every rule is version `1`, and the
mock cites it pinned to that version.

The gate set is one binding, `GateBinding(gate_id="rules", mandatory=False)`,
with no configuration: the rules gate declares no binding model, and every
submission, records included, selects the rule set it is decided under in
its own context, `GateContext(gate_id="rules", gate_config={"ruleset_id":
"mock-batch-v1"})`. The registration names no `novel_entity_feature`, so
Tier 3 never holds an exception, and it declares the `mock-batch-account`
native namespace (DR-32).

## The rule set

`rulesets/mock-batch-v1.yaml` (`ruleset_id` `mock-batch-v1`, version `1`,
`reasoner_id: mock-batch-reasoner`, `default_action: ALLOW`):

| Rule | Matches when | Permits |
| --- | --- | --- |
| `flagged-charge-off` | `days_past_due` ≥ 90 | BLOCK, with the control `collections_review` |
| `flagged-delinquent` | `days_past_due` ≥ 60 | HOLD |
| `flagged-returned-payment` | `returned_payments` ≥ 1 | HOLD |
| `flagged-past-due` | `days_past_due` ≥ 30 | CHALLENGE |

Every rule is evaluated: the actions of the rules that match are the gate's
permitted actions, and enforcement takes the most restrictive; when none
matches, the gate permits ALLOW, the rule set's default. The rules read only
features every row carries, because one predicate that cannot evaluate
fails the whole evaluation (`predicate_error`, and the decision is held).
They are not conditioned on `needs_review`: live, the gate sees only the
flagged rows, and the eval's shadow dimension re-decides recorded rows
under the same rules. `reasoner_id` is required: the gate fails closed
(`ruleset_reasoner_mismatch`) on a submission from any other reasoner that
names the rule set.

The file is deployment configuration, not a plugin asset. The rules gate
reads its rule sets from one directory, `RULES_GATE_RULESETS_DIR`, when a
ledger process builds it, so `make prepare-rulesets` stages every member's
`rulesets/` (`RULESET_DIRS`, which this member's `reasoner.mk` extends) in
`outputs/stage/rulesets`:

```bash
make prepare-rulesets
# prepare-rulesets: staged in outputs/stage/rulesets:
# mock-batch-v1.yaml
# export RULES_GATE_RULESETS_DIR=<repo>/outputs/stage/rulesets
export RULES_GATE_RULESETS_DIR=$(pwd)/outputs/stage/rulesets   # before make serve
```

A ledger started without it serves no rule set: the rules gate fails every
exception closed (`unknown_ruleset`), so each one is held, and the run's
report counts them under `exceptions.failures`. The eval driver and the
tests read the member's `rulesets/` directly (`plugin.RULESETS_DIR`), with
no staging.

## How it plugs in

Two entry points in `pyproject.toml`, both named after the `reasoner_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.reasoners` | `mock_batch_reasoner.plugin:PLUGIN` | `app.plugins.discover_plugins` (the registration; its `prompts_dir` and `corpus_dir` are empty) |
| `decisionledger.eval_adapters` | `mock_batch_reasoner.eval.adapter:ADAPTER` | `eval.adapter.load_eval_adapter` (eval harness) |

The ledger runs none of this package's code (DR-31). The mock has no
service, and it never runs the ledger in its own process: its CLI submits
over HTTP, to `MOCK_BATCH_LEDGER_URL` with `MOCK_BATCH_LEDGER_KEY`, which is
the mock's key in the ledger's `DECISIONLEDGER_API_KEYS`
(`mock-batch-reasoner:<key>`).

Infrastructure: none of its own, and no corpus, prompt template, or asset
root: the rules gate it binds reads only the staged rule set. The member
depends on `decisionledger-gate-rules` because its eval driver composes that
gate.

## Three ways to register

| Source | How | `source` label |
| --- | --- | --- |
| Plugin (the default) | The root `reasoners` group installs the member, so a ledger with `DECISIONLEDGER_REASONERS` blank composes it | `plugin:mock_batch_reasoner.plugin` |
| File | `DECISIONLEDGER_REGISTRATIONS=reasoners/mock_batch/registration.yaml`, with the plugin excluded: `DECISIONLEDGER_REASONERS=ato-reasoner,example-reasoner,mock-stream-reasoner` | `file:reasoners/mock_batch/registration.yaml` |
| API | `PUT /api/v1/reasoners/mock-batch-reasoner` with the operator key and the registration as JSON, the plugin excluded the same way | `api` |

All three serve the same registration and the same `registration_version`,
`f81f57ceb103eb8a`. Whichever the source, the ledger needs the mock's key
and the staged rule set; none needs an asset root.

The PUT body is the registration file's one entry as JSON:

```bash
uv run python -m mock_batch_reasoner registration-yaml \
  | uv run python -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)["registrations"][0]))' \
  > /tmp/mock-batch-registration.json
curl -s -X PUT localhost:8000/api/v1/reasoners/mock-batch-reasoner \
  -H "X-DecisionLedger-Key: $DECISIONLEDGER_OPERATOR_KEY" \
  -H 'content-type: application/json' --data @/tmp/mock-batch-registration.json
# Expected: 201, "source": "api", "registration_version": "f81f57ceb103eb8a"
```

**One source.** A reasoner has one source: a ledger that composes the
plugin or reads the file while an API row for `mock-batch-reasoner` exists
refuses to start, and a `PUT` for the mock while its plugin or the file
registers it answers `409 REGISTRATION_SOURCE_IMMUTABLE`. The mock's eval
driver composes the plugin, so `make eval-mocks` refuses too while the row
exists. To switch from the API registration back to the plugin or the
file, delete the row (a development database; the snapshots stay, so the
bundles that cite them still replay):

```sql
delete from decisionledger.reasoner_registry where reasoner_id = 'mock-batch-reasoner';
```

## Command line

```bash
# Score a synthetic day of 25,000 accounts and submit it: the records in
# three batch requests, the exceptions one at a time for the rules gate;
# print what the ledger answered
MOCK_BATCH_LEDGER_URL=http://localhost:8000 MOCK_BATCH_LEDGER_KEY=mb-key make run-mock_batch
# = uv run python -m mock_batch_reasoner run --count 25000

# Print the batch requests' NDJSON instead (the plan goes to stderr), and
# submit nothing; a dry run reads no settings
uv run python -m mock_batch_reasoner run --count 100 --dry-run > /tmp/day.ndjson

# Print the registration file a ledger reads
uv run python -m mock_batch_reasoner registration-yaml
```

The same CLI is installed as the `mock-batch-reasoner` console script.

| `run` flag | Default | Meaning |
| --- | --- | --- |
| `--count` | 25000 | Rows, one per account (at least 5: the first five rows are one of each profile, the fifth flagged) |
| `--seed` | 7 | The day's positions: the same seed gives the same day, whatever the run tag and date |
| `--date` | yesterday (UTC) | The business date reviewed (ISO 8601); every row's `as_of` is 23:59:59 UTC on it |
| `--run-tag` | random | Prefix of this run's row ids (`row-<run_tag>-<n>`), so runs never share an `event_id` |
| `--chunk-size` | 10000 | Lines per batch request, from 1 to 10,000 (the ledger's limit) |
| `--no-provoke` | off | Do not send the first exception in a batch request |
| `--results-out` | none | Write every batch line's result and every exception's outcome here, as NDJSON |
| `--dry-run` | off | Print the batch requests' NDJSON (and a one-line plan on stderr), and submit nothing |

`run` prints a JSON report: the run tag, the business date, the seed, the
provoked row's `event_id`, the counts `rows`, `decided`, and `undecided`,
and `stopped` (why the run stopped part way, or null). Under `batch`: each
request's lines, recorded, and rejected; the recorded lines by action, and
how many were recorded under a HOLD floor because the rule they cite did
not hold (`cited_rule_not_held`); the rejected ones by code, with up to five
rejections (the provoked one first, marked `provoked`); and the first
recorded line of each action. Under `exceptions`: how many the ledger
decided and refused (with a sample of the refusals); the decided ones by
action and by matched rule; how many fell to the rule set's default; how
many the gate failed (with a sample); and the first five decided, each with
its `decision_id`. `--results-out` holds every line and exception, one JSON
object per line (`kind`: `line`, `exception`, or `refused`).

Exit status: 0 when the ledger decided every row; 1 when it left a row
undecided (a rejected line or a refused exception), when the run stopped
part way (a refused or failed batch request, or the ledger unreachable; the
report still lists what it answered), or when the ledger does not serve
this mock's registration version (nothing is sent; the reason is on
stderr); 2 for invalid arguments or settings (a blank
`MOCK_BATCH_LEDGER_URL` or one that is not http(s), a
`MOCK_BATCH_LEDGER_KEY` an HTTP header cannot carry, a `--count` below 5, a
`--chunk-size` outside 1 to 10,000), with the reason.

## The default run

`make run-mock_batch` (25,000 rows, seed 7) has the same shape for every run
tag and date:

| What | Count |
| --- | --- |
| Batch requests | 3 (10,000, 10,000, and 4,759 lines) |
| Recorded ALLOW under `current-account` | 20,258 |
| Recorded CHALLENGE under `watch-list` | 2,531 |
| Recorded HOLD under `delinquent-hold` | 1,241 |
| Recorded BLOCK under `charge-off` | 728 |
| Rejected `BATCH_RECORD_ONLY` (the provoked line) | 1 |
| Exceptions, decided by the rules gate | 242 |

The provoked line is `row-<run_tag>-000005`, line 5 of the first request:
the fifth row is the day's first flagged account. Of the 242 exceptions, 68
match every rule and are blocked (`flagged-charge-off`), 62 are held
(`flagged-delinquent` with `flagged-past-due`, or `flagged-returned-payment`
alone), 70 are challenged (`flagged-past-due` alone), and 42 match no rule
and are allowed by the default, the provoked row among them. A recorded line
replays under its bypass rule, with the rules gate skipped; an exception
replays on the rules gate's stored output, and its verify answers one
reference per matched rule (or one for the default), `source_id`
`mock-batch-v1@1`, escrow `present`, status `intact`.

## Make targets (run from the repo root)

| Target | What it does |
| --- | --- |
| `make eval-mock_batch` | The framework eval harness with this adapter and the rules gate over the member's rule set; part of `make eval-mocks`, never `make eval` |
| `make run-mock_batch` | Score a synthetic day and submit it (`COUNT=25000`; `MOCK_BATCH_LEDGER_URL`, `MOCK_BATCH_LEDGER_KEY`) |
| `make registration-mock_batch` | Regenerate `registration.yaml` from `registry.py` |

The mock ships no corpus and trains nothing, so it adds no prepare target,
and `make prepare-reasoners` does nothing for it. Its `rulesets/` joins
`RULESET_DIRS`, which `make prepare-rulesets` stages.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOCK_BATCH_LEDGER_URL` | blank | The ledger `run` submits to; required (blank, or not http(s), refuses to run) |
| `MOCK_BATCH_LEDGER_KEY` | blank | The mock's key on that ledger (blank: the ledger answers 401 and the run stops) |

Read from the environment and `.env` by `run`, never at import, so the
plugin and the eval adapter import with no environment at all. A dry run and
`registration-yaml` read neither. The ledger the mock submits to needs the
mock's key in `DECISIONLEDGER_API_KEYS` and the staged rule set in
`RULES_GATE_RULESETS_DIR`.

## Eval

`make eval-mocks` (or `make eval-mock_batch`) runs the framework eval
harness with the mock's adapter: consistency on `scenarios/batch_days.yaml`
(`mb-current-account` → ALLOW, recorded under `current-account`, and
`mb-flagged-account` → HOLD, an exception the rules gate holds, each in two
orderings of one day); robustness on `robustness/malformed.yaml` (a rules
gate that returns no verdict) and `robustness/fallback.yaml` (a rules gate
that raises), two cases each, HOLD; and the report-only shadow dimension,
which re-decides a sample of the run's recorded rows with the rules gate.
The driver composes the ledger over Postgres with the mock's plugin and the
real rules gate over the member's own `rulesets/`, and runs each ordering as
`run` runs a day (`run_day`, without the provocation), so it needs `docker
compose up -d` but no rule-set staging, Elasticsearch, or model call. It
composes the plugin, so run it with no API row for the mock and without
`DECISIONLEDGER_REGISTRATIONS` naming its file. The mock ships no retrieval,
faithfulness, citation, or authority datasets, so the RAG policy gate's own
eval never selects it.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest` with no Docker,
over the in-memory ledger (`tests._memory_ledger`) and the real rules gate
built over the member's rule set
(`RulesGatePlugin(RulesGateSettings(rulesets_dir=...))`, as the eval driver
builds it): the rows, the scorer, the assembler, the synthetic day, the plan
and the run (the provoked `BATCH_RECORD_ONLY` line, asserted on its `code`
and `event_id`; a refused exception and a refused request; a cited rule
that does not hold; a ledger without the rule set; replay and verify), the
rule set (its `reasoner_id`, read with `read_ruleset`, the features it
reads, and the gate's verdict on each kind of flagged account), the plugin
and its make include, the CLI, the eval adapter, datasets, and driver, and
the registration file against `registry.py` (one `registration_version`,
by file and as the `PUT` body).
One `@pytest.mark.smoke` test (`make test-smoke`) runs a synthetic day end
to end, still without Docker, and replays a record and an exception.
