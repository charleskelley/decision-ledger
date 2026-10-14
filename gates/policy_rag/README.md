# decisionledger-gate-policy-rag

The reference retrieval-augmented policy gate for DecisionLedger, packaged
as a uv workspace member. Import package: `policy_rag_gate`; distribution:
`decisionledger-gate-policy-rag`; `gate_id`: `policy`.

For each decision routed to it, the gate runs hybrid retrieval (pgvector
dense + Elasticsearch BM25, fused with RRF, reranked by a cross-encoder with
a latency-budget bypass) over the submitting reasoner's policy corpus,
renders that reasoner's versioned prompt template with the retrieved
snippets, and asks an LLM for a strictly structured verdict. It returns the
framework's universal `GateOutput`: the verdict enforcement reads, one
evidence reference per retrieved snippet (carrying its jurisdiction), and
its own record in the opaque extension — the retrieval and the exact prompt
in `evidence_snapshot` (`PolicyGateSnapshot`), the rationale, citations, and
token cost in `extra` (`PolicyGateExtra`). It is non-deterministic
(`deterministic=False`), so enforcement applies every gate-level tier to it.
Any failure to reach a verdict holds the decision. The framework
(`decision-ledger`) never imports or depends on it.

See DR-28 in `docs/design/decisions.md` for the gate contract.

## How it plugs in

One entry point in `pyproject.toml`, named after the `gate_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.gates` | `policy_rag_gate.plugin:PLUGIN` | `app.gates.discover_gates` (runtime: identity, `config_model`, `build`) |

The root workspace installs it through the `gates` dependency group (a
default group, with the member's `eval` extra), so a bare `uv sync` at the
repo root is enough. Entry points refresh only on reinstall: re-run
`uv sync` after editing this `pyproject.toml`.

## What a reasoner sends

A reasoner routes a decision to the gate with a gate context whose
`gate_config` is a `PolicyGateConfig` (the gate's `config_model`, validated
by the framework at intake on every route):

```python
GateContext(
    gate_id="policy",
    gate_config={
        "template_id": "example-v1",          # a template the reasoner ships
        "template_vars": {"score": "0.61"},   # its placeholders
        "retrieval_query": "step-up authentication after a new device",
        "jurisdictions": ["REGION_A"],        # retrieval filter (both stores)
        "risk_tier": "STANDARD",              # retrieval filter (dense store)
        "corpus_version": "example-corpus-v1",
    },
)
```

Who may send what is decided by the gate, not the reasoner's registration:
when the gate is built it derives a `ReasonerProfile` for every composed
reasoner — the templates in its `prompts_dir` and the jurisdictions in its
`corpus_dir` front matter — and the framework calls the gate's
`authorize_config` at intake. A reasoner may render only templates it ships
and filter only to jurisdictions its own corpus covers.

## Infrastructure

The gate owns all of it (DR-27: the framework's only infrastructure is
Postgres):

- **Postgres** — the chunk table lives in the gate's own `policy_rag` schema
  inside the framework's database. The DDL in `src/policy_rag_gate/sql/`
  (vector extension, schema, table, indexes) is idempotent and applied by
  the plugin on every build and by the corpus loader before every load
  (`make prepare-gate-policy_rag` applies it on its own).
- **Elasticsearch** — `compose.yaml` here, pulled into the root stack by
  `include:`; the index mapping ships in `src/policy_rag_gate/es/`. Point the
  gate at it with `POLICY_RAG_GATE_ELASTICSEARCH_URL`.
- **Models** — `all-MiniLM-L6-v2` embeddings and the
  `ms-marco-MiniLM-L-6-v2` cross-encoder (downloaded on first use).
- **LLM** — the OpenAI adapter for the gate, the Anthropic adapter for the
  eval judges (`src/policy_rag_gate/llm/`; `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`).

## Indexing a reasoner's corpus

```bash
make build-policy-index-<reasoner_id>   # e.g. make build-policy-index-ato-reasoner
# same as:
uv run python -m policy_rag_gate.corpus_loader --reasoner-id <reasoner_id>
```

The loader resolves the corpus directory from the installed reasoner plugin
with that `reasoner_id` (`--corpus-dir` overrides), chunks and embeds every
Markdown document, and writes both stores; `--wipe` clears that reasoner's
chunks first.

## Eval

The gate owns its eval dimensions — retrieval quality, generation
faithfulness, and citation accuracy, with the LLM judge and the RAGAS
scorer — in `src/policy_rag_gate/eval/`. They run against a reasoner's
golden datasets (the reasoner's `EvalAdapter.datasets_dir`):

```bash
make eval-gate-policy_rag                     # every installed reasoner with datasets for this gate
make eval-gate-policy_rag REASONER=<id>       # one reasoner
```

Reports land in `outputs/stage/eval/gate/policy/<reasoner_id>/eval-report.json`.
The run is slow and makes paid LLM calls.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest`. Unit tests need
no Docker: they use the fixture prompt and corpus under `tests/fixtures/`
and fake stores. `tests/integration/` needs the stack (`docker compose up
-d`) and indexes the fixture corpus under its own tenant, cleaning up after
itself. Per project rules the LLM path is not unit-tested with mocked
responses; the eval harness is its test suite.
