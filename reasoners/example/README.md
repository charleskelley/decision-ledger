# example-reasoner

The documented reference reasoner for DecisionLedger, packaged as a uv
workspace member. Import package: `example_reasoner`; distribution:
`example-reasoner`; `reasoner_id`: `example-reasoner`.

It is the framework's test-only stub reasoner (`tests/_stub_reasoner`)
promoted to a full reasoner. The stub stays minimal for framework tests;
this package is the example to read and copy. The domain is deliberately
imaginary: widgets inspected in two regions and scored by a transparent
heuristic, so nothing here can be mistaken for production behavior and
every part of the reasoner contract fits on a screen.

| Module | What it shows |
| --- | --- |
| `events.py` | The reasoner's own event (`WidgetEvent`); it names the widget as its native subject, `(example-widget, widget_id)`, and derives `entity_id` (a UUID5 of `widget_id`, type `widget`) under that declared namespace. |
| `scorer.py` | A pure scorer: `widget_score = logistic(-3 + 4·wobble + 0.5·misalignment_mm)` and the complete feature snapshot. |
| `registry.py` | The registration: the RAG policy gate (advisory), the `low-score` (< 0.2 → record ALLOW) and `high-score` (> 0.9 → record BLOCK) bypass rules, the `example-widget` native namespace, and the enforcement profile (Tier 3 on `thin_history`; Tier 5 on REGION_A vs. REGION_B evidence). |
| `assembler.py` | The handoff: records a widget under the bypass rule that holds, or asks the ledger to gate it, addressing the policy gate on every submission. |
| `pipeline.py` | `decide_widget(event, client=...)`: score, assemble, submit through a `LedgerClient`. |
| `plugin.py` | What a ledger deployment discovers: the registration, `prompts/`, and `corpus/`. |
| `prompts/example-v1.yaml` | The policy gate's template (immutable; a change is a new file). |
| `corpus/` | Three policy documents: a REGION_A release policy, REGION_B guidance, and a GLOBAL escalation standard. |
| `eval/` | The `EvalAdapter`, an in-process driver, and its datasets (consistency; robustness schema-failure and fallback). |
| `__main__.py` | A CLI that prints sample submissions and NDJSON batches and submits widgets. |

## How it plugs in

Two entry points in `pyproject.toml`, both named after the `reasoner_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.reasoners` | `example_reasoner.plugin:PLUGIN` | `app.plugins.discover_plugins` (the registration; the policy gate reads `prompts_dir` and `corpus_dir`) |
| `decisionledger.eval_adapters` | `example_reasoner.eval.adapter:ADAPTER` | `eval.adapter.load_eval_adapter` (eval harness) |

The ledger runs none of this package's code (DR-31). The example submits
the way any reasoner does:

```python
from app.client import HttpClient, LocalClient
from example_reasoner.pipeline import decide_widget

# Over HTTP, with the example's API key (DECISIONLEDGER_API_KEYS on the ledger)
async with HttpClient("http://localhost:8000", api_key=key) as ledger:
    receipt = await decide_widget(event, client=ledger)

# Or with the ledger in this process (no key: the trusted in-process caller)
async with LocalClient.from_settings() as ledger:
    receipt = await decide_widget(event, client=ledger)
```

A reasoner that is not a Python package installed next to the ledger
registers through the ledger's registration file instead
(`DECISIONLEDGER_REGISTRATIONS`) and posts the same JSON; see
`schemas/submission-v2.json` and the `conformance/` suite.

Infrastructure: none of its own. Its gated widgets go to the RAG policy
gate, which needs its corpus indexed (`make prepare-example`).

## The subject

The example names the widget it scores as its native subject (DR-32),
`(example-widget, widget_id)`, and its registration declares that namespace
(`EXAMPLE_WIDGET_NAMESPACE`: subject type `widget`, and the UUID5 namespace
the example's widget ids have always derived under). Intake refuses a
submission whose `entity_id` is not that declaration's derivation of its
`widget_id` (422 `NATIVE_SUBJECT_MISMATCH`). With no identity resolver
active, the ledger records each decision under the widget's UUID5 and type
`widget`, unresolved, with the reason; a deployment's resolver can map the
widget to the enterprise subject it belongs to. Every inspection of one
widget is on one timeline (`GET /api/v1/entities/{entity_id}/decisions`).
The ATO reasoner names its accounts the same way.

## Command line

```bash
uv run python -m example_reasoner sample --band mid > widget.json     # one submission
uv run python -m example_reasoner batch --count 1000 > widgets.ndjson # NDJSON records
uv run python -m example_reasoner submit --band low --ledger-url http://localhost:8000 --key "$KEY"
uv run python -m example_reasoner submit --band low --in-process   # the ledger in this process (its stack must be up)
```

The same CLI is installed as the `example-reasoner` console script.

`low` scores ≈ 0.06 (recorded ALLOW), `mid` 0.50 (gated by the policy
gate), `high` ≈ 0.98 (recorded BLOCK). A `batch` mixes low (ALLOW) and
high (BLOCK) records over 50 widgets.

## Make targets (run from the repo root)

| Target | What it does |
| --- | --- |
| `make build-policy-index-example-reasoner` | Index `corpus/` into the RAG policy gate under `reasoner_id=example-reasoner` |
| `make prepare-example` | The same (the scorer needs no training) |
| `make eval-example` | The framework eval harness with this adapter (the shadow dimension re-decides recorded widgets through the LLM gate: paid) |
| `make sample-example BAND=mid` | Print one wire submission |
| `make batch-example COUNT=1000` | Print an NDJSON batch of records |

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest` with no Docker.
