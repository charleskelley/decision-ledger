# decisionledger-gate-policy-rag

The reference retrieval-augmented policy gate for DecisionLedger, packaged
as a uv workspace member. Import package: `policy_rag_gate`; distribution:
`decisionledger-gate-policy-rag`; `gate_id`: `policy`; kind: `llm`.

For each decision addressed to it, the gate runs hybrid retrieval (pgvector
dense and Elasticsearch BM25, fused with reciprocal rank fusion, reranked by
a cross-encoder with a latency-budget bypass) over the submitting reasoner's
policy corpus, renders that reasoner's versioned prompt template with the
retrieved snippets, and asks an LLM for a strictly structured verdict. It
returns the framework's universal `GateOutput`: the verdict enforcement
reads, one evidence reference per retrieved snippet (carrying its
jurisdiction), and its own record in the opaque extension: the retrieval and
the exact prompt in `evidence_snapshot` (`PolicyGateSnapshot`), and the
rationale, citations, token cost, retrieval latency, and any failing stage in
`extra` (`PolicyGateExtra`). It is non-deterministic (`deterministic=False`),
so enforcement applies every gate-level tier to it.

A missing retrieval query, a template render error, or a model error returns
no verdict, which enforcement holds. An unknown template or a retrieval-store
error raises, and the ledger records the gate as failed, which holds the
decision too. The framework (`decision-ledger`) never imports or depends on
this package.

See [Policy gate](../../docs/design/gates/policy/index.md) for the design and
[How to implement a gate](../../docs/design/gates/implementation.md) for the
contract (DR-28 in `docs/design/decisions.md`).

## How it plugs in

One entry point in `pyproject.toml`, named after the `gate_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.gates` | `policy_rag_gate.plugin:PLUGIN` | `app.gates.discover_gates` (runtime: identity, `config_model`, `build`) |

The root workspace installs it through the `gates` dependency group (a
default group, with the member's `eval` extra: RAGAS and `langchain-openai`),
so a bare `uv sync` at the repo root is enough. Entry points refresh only on
reinstall: re-run `uv sync` after editing this `pyproject.toml`.

`build` derives a profile per composed reasoner plugin, loads every
reasoner's prompt templates, opens its own autocommit Postgres connection to
the framework's database and applies its DDL, and builds the retriever, the
models, and the OpenAI client. It opens no Elasticsearch connection, so a
deployment whose decisions never reach the gate runs with Elasticsearch
down.

## What a reasoner sends

A reasoner addresses the gate with a gate context whose `gate_config`
satisfies `PolicyGateConfig` (the gate's `config_model`, validated by the
framework at intake on every route, recorded decisions included). The
example reasoner sends this shape (values illustrative):

```python
GateContext(
    gate_id="policy",
    gate_config={
        "template_id": "example-v1",                 # a template the reasoner ships
        "template_vars": {"widget_score": "0.612", "region": "REGION_A"},  # its placeholders
        "retrieval_query": "widget inspection in REGION_A: ...",
        "jurisdictions": ["REGION_A", "GLOBAL"],     # retrieval filter (both stores)
        "corpus_version": "example-v1",              # recorded in the snapshot
    },
)
```

`risk_tier` (a filter on the dense store only) is optional. Unknown keys are
rejected (`422 GATE_CONFIG_INVALID`).

Who may send what is decided by the gate, not by the reasoner's
registration. When the gate is built it derives a `ReasonerProfile` for every
composed reasoner from what it ships: the template ids in its `prompts_dir`
and the `jurisdiction` front matter of its `corpus_dir` documents. The built
gate is a `ConfigAuthorizer`: at intake a reasoner may render only templates
it ships and filter only to jurisdictions its own corpus covers
(`422 GATE_CONFIG_UNAUTHORIZED` otherwise). It is also a `ReasonerScope`: it
serves only the reasoners it built a profile for, so a reasoner registered
through the registration file, with no plugin installed, cannot bind it
(startup refuses the deployment).

## Infrastructure

The gate owns all of it (DR-28; the framework's only infrastructure is
Postgres):

- **Postgres.** The chunk table `policy_rag.policy_chunks` lives in the
  gate's own `policy_rag` schema inside the framework's database. The DDL in
  `src/policy_rag_gate/sql/` (vector extension, schema, table, indexes) is
  idempotent and applied by the plugin on every build and by the corpus
  loader before every load; `make prepare-gate-policy_rag`
  (`uv run python -m policy_rag_gate.schema`) applies it on its own. It never
  touches the framework's `decisionledger` schema.
- **Elasticsearch.** `compose.yaml` here, pulled into the root stack by
  `include:`, published on `POLICY_RAG_GATE_ELASTICSEARCH_PORT` (default
  9200). The index mapping ships in `src/policy_rag_gate/es/`.
- **Models.** `all-MiniLM-L6-v2` embeddings and the
  `cross-encoder/ms-marco-MiniLM-L-6-v2` cross-encoder (downloaded on first
  use).
- **LLM.** The OpenAI adapter for the gate and the Anthropic adapter for the
  eval judges, in `src/policy_rag_gate/llm/`, the only place in the
  repository an LLM SDK is imported.

| Setting | Default | Used for |
| --- | --- | --- |
| `POLICY_RAG_GATE_ELASTICSEARCH_URL` | `http://localhost:9200` | The BM25 store |
| `OPENAI_API_KEY` | none | The gate's model calls; RAGAS in the eval |
| `ANTHROPIC_API_KEY` | none | The eval's judges |

The settings class is `PolicyRagGateSettings` (prefix `POLICY_RAG_GATE_`;
the provider keys keep their providers' names). The Postgres DSN comes from
the framework's settings.

## Indexing a reasoner's corpus

```bash
make build-policy-index-<reasoner_id>   # e.g. make build-policy-index-ato-reasoner
# same as:
uv run python -m policy_rag_gate.corpus_loader --reasoner-id <reasoner_id>
```

The loader resolves the corpus directory from the installed reasoner plugin
with that `reasoner_id` (`--corpus-dir` overrides), chunks and embeds every
Markdown document, and writes both stores, tagging every chunk with the
`reasoner_id`. `--wipe` first clears that reasoner's chunks from both
stores. Without it the loader replaces each document's rows in Postgres but
deletes nothing in Elasticsearch, so re-indexing an already indexed corpus
without `--wipe` leaves its earlier BM25 documents beside the new ones. See
[Policy corpus](../../docs/design/gates/policy/policy-corpus.md) for the
document format and the chunking rules.

## Eval

The gate owns its eval dimensions (retrieval quality, generation
faithfulness, and citation accuracy, with the LLM judge and the RAGAS
scorer) in `src/policy_rag_gate/eval/`. They run against the golden datasets
a reasoner ships for the gate under its `EvalAdapter.datasets_dir`
(`retrieval/golden_queries.yaml`, `faithfulness/golden_outputs.yaml`,
`citations/golden_outputs.yaml`):

```bash
make eval-gate-policy_rag                     # every installed reasoner that ships datasets for this gate
make eval-gate-policy_rag REASONER=<id>       # one reasoner
uv run python -m policy_rag_gate.eval [--reasoner-id <id>] [--output-dir <dir>]
```

Reports land in `outputs/stage/eval/gate/policy/<reasoner_id>/eval-report.json`.
The run is slow and makes paid LLM calls; it needs `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` (exit 2 without them) and the reasoner's corpus indexed.
See [Evaluation](../../docs/design/evaluation.md) for the thresholds.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest`. Unit tests need
no Docker: they use the fixture prompt and corpus under `tests/fixtures/`
and fake stores, and import nothing from the framework's `tests/` tree.
`tests/integration/` needs the stack (`docker compose up -d`) and indexes
the fixture corpus under its own tenant, cleaning up after itself. Per
project rules the LLM path is not unit-tested with mocked responses; the
eval harness is its test suite.
