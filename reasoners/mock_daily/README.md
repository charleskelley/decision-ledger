# mock-daily-reasoner

The deterministic computation with exceptions pattern for DecisionLedger,
packaged as a uv workspace member. Import package: `mock_daily_reasoner`;
distribution: `mock-daily-reasoner`; `reasoner_id`: `mock-daily-reasoner`.

It is one of the reference reasoner patterns (DR-35): the repository carries
one mock reasoner per valid reasoner pattern, so the framework's contracts
are demonstrated and tested in place. A mock demonstrates contracts; it is
not a product. The domain is imaginary: once a day, the mock reconciles each
account's settlement, and pure arithmetic over a target (what the books
expect to settle to the account) and an actual (what the settlement file
reports) gives the drift between them. It records every account within
tolerance under its one bypass rule and submits the rest, its exceptions, in
gate mode, one decision per request. The rules gate is bound mandatory, so it
runs on every decision, recorded ones too: it decides the exceptions by the
size of their drift, and its verdict joins the combinator with each record's
own action. It has no service and no infrastructure of its own: its CLI
reaches the ledger over HTTP, and the ledger never imports it, whichever way
it is registered (its plugin, a registration file, or a `PUT`). The design
page is `docs/design/reasoners/patterns.md`.

| Module | What it shows |
| --- | --- |
| `settlements.py` | The mock's own row (`AccountSettlement`: `event_id`, an aware `as_of`, `account_id`, `target_amount` > 0, what the books expect to settle, and `actual_amount` ≥ 0, what the settlement file reports; strict, unknown fields refused, every value one the ledger can store). It names the account as its native subject, `(mock-daily-account, account_id)`, and derives `entity_id` (a UUID5 of `account_id`, type `account`) under that declared namespace, so every day's row for an account is about one ledger subject. |
| `compute.py` | A pure computation, not a model: `compute_drift` gives `drift_amount` (the actual less the target, to the cent), `drift_pct` (`100 * (actual - target) / target`, to four places; negative for a shortfall), and `abs_drift_pct`, with every value it read. It reads one row alone, so the order of a day's rows never changes a result. `COMPUTE_VERSION` is `settlement-drift-v1`. |
| `registry.py` | The registration: the rules gate (mandatory, no binding configuration), one bypass rule (`within-tolerance`: `abs_drift_pct` below `TOLERANCE_PCT`, 1.0), ALLOW the only recordable action, and the `mock-daily-account` native namespace. `RULESET_ID` is the rule set every submission names; `registration_yaml()` writes the registration file. |
| `assembler.py` | The handoff: records an account within tolerance (ALLOW, citing `within-tolerance` pinned to its version), or submits it in gate mode with no proposal, as an exception. Every submission, records included, carries the rules gate's context, `{"ruleset_id": "mock-daily-v1"}`, and the computation as a numerical label (`label_name` `drift_pct`), its complete feature set, and its arithmetic as the attribution's narrative. |
| `daily.py` | `compute_day` (it refuses a day that repeats an `event_id`), `run_day`, which submits each row on its own and reads its bundle back, `registration_problem`, which a run checks first, and the run (`DayRun`: the report, and every decision's outcome). |
| `synthetic.py` | `synthetic_day`: a deterministic day for a seed, one row per account (`acct-000001` onward, the same accounts every day), with row ids under the run tag (`set-<run_tag>-<n>`). |
| `plugin.py` | What a ledger deployment that installs the member discovers: the registration, and empty `prompts/` and `corpus/` directories. `RULESETS_DIR` is the member's `rulesets/` in the workspace checkout. |
| `settings.py` | `MockDailySettings` (`MOCK_DAILY_` prefix): the ledger URL (required) and key. |
| `eval/` | The `EvalAdapter`, an in-process driver over the real rules gate, and its datasets (consistency; robustness schema failure and fallback). |
| `__main__.py` | The CLI: `run`, `registration-yaml`. |

`registration.yaml` at the member root is the registration file, generated
from `registry.py` (`make registration-mock_daily`); do not edit it by
hand: a test holds the two to one `registration_version`.
`rulesets/mock-daily-v1.yaml`, also at the member root, is the rule set the
rules gate decides every decision under ([below](#the-rule-set)).

## What it proves

- **A computation whose feature set is complete.** The bypass rule, the
  rules gate, and replay read nothing but the values `compute_drift` read
  and gave, so a bundle is self-contained.
- **A mandatory gate on every decision.** The rules gate runs on the records
  too: it permits the rule set's default on a record, a rule that fires on a
  record raises its action, and a gate that fails holds it.
- **A rule set that starts where the bypass rule ends.** Every exception is
  decided by a rule, by the size of its drift, and a BLOCK carries the
  required control `settlement_investigation`.
- **One decision per request.** Every row goes to `POST /api/v1/decisions`
  on its own, and every bundle is read back; the mock uses no batch
  endpoint.
- **Replay determinism.** Every decision replays matched, recorded or gated:
  replay re-enforces the rules gate's stored output under the registration
  snapshot the bundle names.
- **A registration that asks the deployment for its gate.** A ledger that
  does not compose the rules gate refuses the registration, however it is
  registered.

## How a day is decided

`run_day` computes every row's drift and assembles its submission, then, in
row order, one row at a time:

1. **Submit.** A row whose `abs_drift_pct` is below 1.0 is a `record`
   submission: ALLOW, citing `within-tolerance`. Any other row is an
   exception, a `gate` submission with no proposal. Each goes to
   `POST /api/v1/decisions` on its own, recorded rows included: the batch
   endpoint is the batch mock's pattern, and here every decision is one
   request whose answer names its gate.
2. **The rules gate, on every decision.** A record runs only the mandatory
   gates, and the mock binds the rules gate mandatory, so it runs on every
   row, under `mock-daily-v1`. On a recorded row it matches no rule and
   permits the rule set's default, ALLOW, so the record keeps its action; on
   an exception its rules decide by the size of the drift. Its verdict joins
   the combinator with the record's own action, so a rule that fired on a
   recorded row would raise it, and a rules gate that fails holds the
   decision, recorded or not (Tier 1).
3. **Read back.** The mock reads each decision's bundle
   (`GET /api/v1/decisions/{decision_id}`): the gate's status, the rules
   that matched, whether the rule set's default applied, and any gate
   failure, for the recorded rows as for the exceptions.

What the ledger does with a row, and what the mock does about it:

- **A cited rule that does not hold is a recorded HOLD, not a refusal.** The
  ledger stores the row with `bypass_rule_holds: false` under a HOLD floor
  (decision-level rule `bypass_rule_not_satisfied`). The mock cites its rule
  only when it holds under its own registration; a ledger serving another
  version of it could disagree, so `run` first checks that the ledger serves
  the same `registration_version` and sends nothing otherwise, and the
  report counts any row recorded this way (`recorded.cited_rule_not_held`).
- **No deduplication.** The ledger decides every row as a new decision,
  whatever its `event_id`, so a day must not repeat one (`compute_day`
  refuses it), and a day sent twice is decided twice. The CLI's run tag,
  random by default, keeps runs apart.
- **Failures.** A row the ledger refuses for itself (a 4xx problem about
  the submission) is reported, not retried, and the run goes on. A refusal
  of the key (401 or 403: another reasoner's key, or the operator's, may read
  the mock's registration but not write its decisions), a server error
  (5xx), a ledger that cannot be reached, or a failed read of a decided
  row's bundle stops the run: `run_day` raises `DayRunStopped`, which
  carries everything the ledger answered until then. The ledger does not
  deduplicate, so the operator must know every row a stopped run may have
  decided: a row decided before its bundle read failed is kept among the
  decisions, from its receipt (`bundle_read` false: the gate's status and
  any failure the receipt names, but no matched rules or default), and a
  row whose request was sent but not answered (a 5xx, or a transport error
  after sending) is named `in_doubt`, to be looked up before it is sent
  again. The CLI still prints the report (with `stopped`, the error) and
  writes `--results-out`.
- **A rules gate without a verdict.** A decision the rules gate gave no
  verdict on is held, recorded or not, and counted under `failures`; `run`
  then exits 1. A ledger that does not hold the mock's rule set (a blank or
  unstaged `RULES_GATE_RULESETS_DIR`) holds every decision this way
  (`unknown_ruleset`).

## Registration

| Bypass rule | Holds when | Records |
| --- | --- | --- |
| `within-tolerance` | `abs_drift_pct` below 1.0 (`TOLERANCE_PCT`): the account settled within 1% of its target, short or over | ALLOW (release the settlement) |

The rule is version `1`, and the mock cites it pinned to that version. ALLOW
is the only recordable action, so every other account is an exception,
gated with no proposal: the rules gate alone decides it.

The gate set is one binding, `GateBinding(gate_id="rules", mandatory=True)`,
with no configuration: the rules gate declares no binding model, and every
submission, records included, selects the rule set it is decided under in
its own context, `GateContext(gate_id="rules", gate_config={"ruleset_id":
"mock-daily-v1"})`. Bound mandatory, the gate runs on every decision, and a
ledger serves the registration only when it composes the gate: a ledger
that does not refuses to start when it composes the plugin or reads the
registration file, and answers a `PUT` of the registration with
`422 GATE_BINDING_REFUSED`. The registration names no
`novel_entity_feature`, so Tier 3 never fires, and it declares the
`mock-daily-account` native namespace (DR-32; entity type `account`).

## The rule set

`rulesets/mock-daily-v1.yaml` (`ruleset_id` `mock-daily-v1`, version `1`,
`reasoner_id: mock-daily-reasoner`, `default_action: ALLOW`):

| Rule | Matches when | Permits |
| --- | --- | --- |
| `drift-severe` | `abs_drift_pct` ≥ 20 | BLOCK, with the control `settlement_investigation` |
| `drift-major` | `abs_drift_pct` ≥ 5 | HOLD |
| `drift-minor` | `abs_drift_pct` ≥ 1 | CHALLENGE |

Every rule is evaluated: the actions of the rules that match are the gate's
permitted actions, and enforcement takes the most restrictive; when none
matches, the gate permits ALLOW, the rule set's default. The rule set starts
where the bypass rule ends: `drift-minor` fires from the bypass rule's
tolerance, so a recorded account matches no rule and keeps its ALLOW, and
every exception matches at least one rule (a test holds the two thresholds
together over a grid that includes each threshold, short and over). The
rules read only `abs_drift_pct`, which every row carries, because one
predicate that cannot evaluate fails the whole evaluation (`predicate_error`,
and the decision is held); a shortfall and an overage of one size are
decided alike. `reasoner_id` is required: the gate fails closed
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
# mock-daily-v1.yaml
# export RULES_GATE_RULESETS_DIR=<repo>/outputs/stage/rulesets
export RULES_GATE_RULESETS_DIR=$(pwd)/outputs/stage/rulesets   # before make serve
```

The gate is mandatory, so a ledger started without the rule set holds every
one of the mock's decisions, recorded ones too: the rules gate fails each
closed (`unknown_ruleset`, Tier 1), and the run's report counts them under
`recorded.failures` and `exceptions.failures`, and each held record under
`recorded.raised`; `run` exits 1 and says how to stage the rule set. The eval driver and the tests read the member's
`rulesets/` directly (`plugin.RULESETS_DIR`), with no staging.

## How it plugs in

Two entry points in `pyproject.toml`, both named after the `reasoner_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.reasoners` | `mock_daily_reasoner.plugin:PLUGIN` | `app.plugins.discover_plugins` (the registration; its `prompts_dir` and `corpus_dir` are empty) |
| `decisionledger.eval_adapters` | `mock_daily_reasoner.eval.adapter:ADAPTER` | `eval.adapter.load_eval_adapter` (eval harness) |

The ledger runs none of this package's code (DR-31). The mock has no
service, and it never runs the ledger in its own process: its CLI submits
over HTTP, to `MOCK_DAILY_LEDGER_URL` with `MOCK_DAILY_LEDGER_KEY`, which is
the mock's key in the ledger's `DECISIONLEDGER_API_KEYS`
(`mock-daily-reasoner:<key>`).

Infrastructure: none of its own, and no corpus, prompt template, or asset
root: the rules gate it binds reads only the staged rule set. The member
depends on `decisionledger-gate-rules` because its eval driver composes that
gate.

## Three ways to register

| Source | How | `source` label |
| --- | --- | --- |
| Plugin (the default) | The root `reasoners` group installs the member, so a ledger with `DECISIONLEDGER_REASONERS` blank composes it | `plugin:mock_daily_reasoner.plugin` |
| File | `DECISIONLEDGER_REGISTRATIONS=reasoners/mock_daily/registration.yaml`, with the plugin excluded: `DECISIONLEDGER_REASONERS=ato-reasoner,example-reasoner,mock-batch-reasoner,mock-stream-reasoner,mock-text-reasoner` | `file:reasoners/mock_daily/registration.yaml` |
| API | `PUT /api/v1/reasoners/mock-daily-reasoner` with the operator key and the registration as JSON, the plugin excluded the same way | `api` |

All three serve the same registration and the same `registration_version`,
`4b58a24f15c700fe`. Whichever the source, the ledger needs the rules gate
composed, the mock's key, and the staged rule set; none needs an asset root.

The PUT body is the registration file's one entry as JSON:

```bash
uv run python -m mock_daily_reasoner registration-yaml \
  | uv run python -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)["registrations"][0]))' \
  > /tmp/mock-daily-registration.json
curl -s -X PUT localhost:8000/api/v1/reasoners/mock-daily-reasoner \
  -H "X-DecisionLedger-Key: $DECISIONLEDGER_OPERATOR_KEY" \
  -H 'content-type: application/json' --data @/tmp/mock-daily-registration.json
# Expected: 201, "source": "api", "registration_version": "4b58a24f15c700fe"
```

**One source.** A reasoner has one source: a ledger that composes the
plugin or reads the file while an API row for `mock-daily-reasoner` exists
refuses to start, and a `PUT` for the mock while its plugin or the file
registers it answers `409 REGISTRATION_SOURCE_IMMUTABLE`. The mock's eval
driver composes the plugin, so `make eval-mocks` refuses too while the row
exists. To switch from the API registration back to the plugin or the
file, delete the row (a development database; the snapshots stay, so the
bundles that cite them still replay):

```sql
delete from decisionledger.reasoner_registry where reasoner_id = 'mock-daily-reasoner';
```

## Command line

```bash
# Reconcile a synthetic day of 1,000 accounts and submit it, one decision per
# request: the accounts within tolerance recorded, the exceptions gated, and
# the rules gate on every one; print what the ledger answered
MOCK_DAILY_LEDGER_URL=http://localhost:8000 MOCK_DAILY_LEDGER_KEY=md-key make run-mock_daily
# = uv run python -m mock_daily_reasoner run --count 1000

# Print the submissions as NDJSON instead (a one-line summary goes to
# stderr), and submit nothing; a dry run reads no settings
uv run python -m mock_daily_reasoner run --count 100 --dry-run > /tmp/day.ndjson

# Print the registration file a ledger reads
uv run python -m mock_daily_reasoner registration-yaml
```

The same CLI is installed as the `mock-daily-reasoner` console script.

| `run` flag | Default | Meaning |
| --- | --- | --- |
| `--count` | 1000 | Rows, one per account (at least 4: the first four rows are one of each profile) |
| `--seed` | 11 | The day's amounts: the same seed gives the same amounts, whatever the run tag and date |
| `--date` | yesterday (UTC) | The business date reconciled (ISO 8601); every row's `as_of` is 23:59:59 UTC on it |
| `--run-tag` | random | Prefix of this run's row ids (`set-<run_tag>-<n>`), so runs never share an `event_id` |
| `--results-out` | none | Write every decision's outcome and every refusal here, as NDJSON |
| `--dry-run` | off | Print the submissions as NDJSON (and a one-line summary on stderr), and submit nothing |

`run` first reads the ledger's registration of the mock and sends nothing
unless it has the same `registration_version`. It then prints a JSON report:
the run tag, the business date, the seed, the counts `rows`, `decided`, and
`undecided`, `stopped` (why the run stopped part way, or null), and
`in_doubt` (the row the run stopped on when it was sent but not answered,
or null). Under
`recorded` and under `exceptions`: how many the ledger decided; the decided
ones by action, by the rules gate's status, and by matched rule; how many
fell to the rule set's default; how many the gate gave no verdict on
(`failures`, with up to five in `failure_sample`); and the first five decided
(`sample`), each with its `decision_id`. `recorded` adds `raised`, the
records decided other than their own ALLOW, and `cited_rule_not_held`, the
records stored under the HOLD floor. Then `refused`, how many rows the ledger
refused, with up to five in `refused_sample`. `--results-out` holds one JSON
object per line: one per decision, in row order (`kind` `decision`, with
`event_id`, `decision_id`, `mode`, `proposed_action`, `decision_action`,
`decision_level_rule`, `bypass_rule_holds`, `gate_status`, `matched_rules`,
`default_applied`, `failure`, and `bundle_read`), then one per refusal
(`kind` `refused`, with `event_id`, `status`, `code`, and `detail`).

Exit status: 0 when the ledger decided every row and the rules gate gave a
verdict on each; 1 when it left a row undecided (a refused row), when the
rules gate gave no verdict on a decision (the ledger held it; stderr names
the count and the first reason), when the run stopped part way (a refused
key, a server error, a failed bundle read, or the ledger unreachable; the
report still lists what it answered, and stderr names a row in doubt), or
when the registration check fails: the ledger serves another version of the
mock's registration, does not register the mock, refuses the request (a
missing or unknown key), or cannot be reached (nothing is sent, and the
reason is on stderr); 2 for invalid arguments or settings (a
`--results-out` that cannot be written, checked before anything is sent, a
blank `MOCK_DAILY_LEDGER_URL`, one that is not http(s) or that httpx cannot
parse, a `MOCK_DAILY_LEDGER_KEY` an HTTP header cannot carry, a `--count`
below 4, a `--date` that is not ISO 8601), with the reason.

## The default run

`make run-mock_daily` (1,000 rows, seed 11) has the same shape for every run
tag and date:

| What | Count |
| --- | --- |
| Recorded ALLOW under `within-tolerance`, the rules gate's default applied | 939 |
| Exceptions challenged (`drift-minor` alone) | 37 |
| Exceptions held (`drift-major` with `drift-minor`) | 18 |
| Exceptions blocked (all three rules; the control `settlement_investigation`) | 6 |
| Raised records, gate failures, refused rows | 0 |

The rules gate ran on all 1,000 decisions; the exceptions' matched rules
are `drift-minor` 61, `drift-major` 24, and `drift-severe` 6. The first four
rows are one of each profile: `set-<run_tag>-000001` is recorded,
`set-<run_tag>-000002` challenged, `set-<run_tag>-000003` held, and
`set-<run_tag>-000004` blocked. Every decision replays matched: a recorded
row under its bypass rule and the rules gate's stored output, an exception
on the gate's stored output. Each decision's verify answers one reference
per matched rule, or one for the rule set's default on a recorded row
(`"locator": "default_action"`), each with `source_id` `mock-daily-v1@1`,
escrow `present`, and status `intact`.

## Make targets (run from the repo root)

| Target | What it does |
| --- | --- |
| `make eval-mock_daily` | The framework eval harness with this adapter and the rules gate over the member's rule set; part of `make eval-mocks`, never `make eval` |
| `make run-mock_daily` | Reconcile a synthetic day and submit it (`COUNT=1000`; `MOCK_DAILY_LEDGER_URL`, `MOCK_DAILY_LEDGER_KEY`) |
| `make registration-mock_daily` | Regenerate `registration.yaml` from `registry.py` |

The mock ships no corpus and trains nothing, so it adds no prepare target,
and `make prepare-reasoners` does nothing for it. Its `rulesets/` joins
`RULESET_DIRS`, which `make prepare-rulesets` stages.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOCK_DAILY_LEDGER_URL` | blank | The ledger `run` submits to; required (blank, or not http(s), refuses to run) |
| `MOCK_DAILY_LEDGER_KEY` | blank | The mock's key on that ledger (blank: the ledger answers 401 and the run stops before sending anything) |

Read from the environment and `.env` by `run`, never at import, so the
plugin and the eval adapter import with no environment at all. A dry run and
`registration-yaml` read neither. The ledger the mock submits to needs the
rules gate composed, the mock's key in `DECISIONLEDGER_API_KEYS`, and the
staged rule set in `RULES_GATE_RULESETS_DIR`.

## Eval

`make eval-mocks` (or `make eval-mock_daily`) runs the framework eval
harness with the mock's adapter: consistency on
`scenarios/settlement_days.yaml` (`md-within-tolerance` → ALLOW, an account
0.4% short of its target, recorded under `within-tolerance` with the rules
gate's default, and `md-major-drift` → HOLD, an account 8% short, an
exception that `drift-major` and `drift-minor` match, each in two orderings
of a day that holds records and exceptions alike); robustness on
`robustness/malformed.yaml` (a mandatory rules-gate record with no verdict,
enforced in the row's own mode) and `robustness/fallback.yaml` (the rules
gate swapped for one that raises), each with a recorded row and an
exception, all four HOLD; and the report-only shadow dimension, which
re-decides recorded decisions with their advisory gates and so re-decides
none here (`num_samples` 0): the mock's only gate is mandatory and already
ran on every record. The driver composes the ledger over Postgres with the
mock's plugin and the real rules gate over the member's own `rulesets/`,
resets its state at each ordering under a lock, and runs each ordering as
`run` runs a day (`run_day`), so it needs `docker compose up -d` but no
rule-set staging, Elasticsearch, or model call. It reports the gate's
verdict for every trigger, recorded or gated (confidence 1.0, rationale
`rules gate: <the matched rules, or the default action>`). It composes the
plugin, so run it with no API row for the mock and without
`DECISIONLEDGER_REGISTRATIONS` naming its file. The mock ships no retrieval,
faithfulness, citation, or authority datasets, so the RAG policy gate's own
eval never selects it.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest` with no Docker,
over the in-memory ledger (`tests._memory_ledger`) and the real rules gate
built over the member's rule set
(`RulesGatePlugin(RulesGateSettings(rulesets_dir=...))`, as the eval driver
builds it): the row (strict, closed, storable, and its native subject); the
computation (the arithmetic, its rounding and sign, a complete feature set,
and the narrative); the synthetic day (deterministic, every band, the first
four rows); the assembler (the mode the bypass rule picks, and the rules
gate's context on every submission); the rule set (its `reasoner_id`, read
with `read_ruleset`, against the plugin's; the one field it reads; its
thresholds against the bypass rule's over a grid that includes each
threshold; and the gate's verdict on each band of drift, through the
ledger); the run (one request per row, in row order, each bundle read back;
a refused row; a refused key and a server error, which stop it; a stopped
run that keeps the row it decided, with the failure its receipt names; a
row sent and not answered, named in doubt; a cited rule that does not hold;
the report; and the registration check); the mandatory
gate (a 1,000-row day whose every bundle shows the gate ran and whose every
decision replays matched; a temporary rule set with one more rule, which
raises a recorded ALLOW to HOLD while the cited rule holds; and a gate that
raises and a ledger without the rule set, each holding every decision,
recorded ones too); the CLI (a dry run, the report and the results file,
a results file that cannot be written, a URL that cannot be parsed, a row in
doubt, and the exit status, a rules gate without a verdict included); the
plugin and its make include; the eval adapter,
datasets, and driver; and the registration file against `registry.py` (one
`registration_version`, by file and as the `PUT` body; a ledger without the
rules gate refuses it at startup and at the `PUT`). No test needs Postgres,
and none is a smoke test.
