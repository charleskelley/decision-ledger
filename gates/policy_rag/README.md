# decisionledger-gate-policy-rag

The reference retrieval-augmented policy gate for DecisionLedger, packaged
as a uv workspace member. Import package: `policy_rag_gate`; distribution:
`decisionledger-gate-policy-rag`; `gate_id`: `policy`; kind: `llm`.

For each decision addressed to it, the gate retrieves from every corpus the
submitting reasoner's binding names (`PolicyBindingConfig`, DR-34), through
the retrieval provider that holds each one — the reference provider is
hybrid retrieval (pgvector dense and Elasticsearch BM25, fused with
reciprocal rank fusion, reranked by a cross-encoder with a latency-budget
bypass) — the same quota per corpus, never fusing scores across corpora. It
renders that reasoner's versioned prompt template with the passages (one
numbered list, or blocks by authority tier, as the template declares), and
asks an LLM for a strictly structured verdict, holding the decision for
review (a model BLOCK stays a BLOCK) when a tiered verdict reports
disagreeing or insufficient evidence. Before it renders, it escrows in the
ledger (through the framework's store) the whole document each passage
came from, once per decision (DR-34). It returns the
framework's universal `GateOutput`: the verdict enforcement reads, one
evidence reference per rendered passage (carrying its jurisdiction, the
SHA-256 of its text, and the digest of the escrowed document), and its own
record in the opaque extension: the
per-corpus queries, the passages under the evidence contract, and the exact
prompt in `evidence_snapshot` (`PolicyGateSnapshot`, version 2), and the
rationale, citations, conflict/gap report, token cost, retrieval latency,
and any failing stage in `extra` (`PolicyGateExtra`). It is
non-deterministic (`deterministic=False`), so enforcement applies every
gate-level tier to it.

A missing retrieval query, a template render error, or a model error returns
no verdict, which enforcement holds. An unknown template, a provider error,
or a provider answer that breaks the evidence contract raises, and the
ledger records the gate as failed, which holds the decision too, as does a
document it cannot fetch or escrow. The framework (`decision-ledger`) never
imports or depends on this package.

See [Policy gate](../../docs/design/gates/policy/index.md) for the design and
[How to implement a gate](../../docs/design/gates/implementation.md) for the
contract (DR-28 in `docs/design/decisions.md`).

## How it plugs in

Two entry points in `pyproject.toml`: the gate, named after the `gate_id`,
and the reference retrieval provider, in the gate's own group:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.gates` | `policy_rag_gate.plugin:PLUGIN` | `app.gates.discover_gates` (runtime: identity, `config_model`, `binding_config_model`, `build`) |
| `policy_rag_gate.providers` | `reference = policy_rag_gate.providers.reference:PLUGIN` | the gate's `ProviderRegistry` (a provider is built the first time a binding names it) |

The root workspace installs it through the `gates` dependency group (a
default group, with the member's `eval` extra: RAGAS and `langchain-openai`),
so a bare `uv sync` at the repo root is enough. Entry points refresh only on
reinstall: re-run `uv sync` after editing this `pyproject.toml`.

`build` derives a profile per composed reasoner plugin, loads every
plugin reasoner's prompt templates, discovers the retrieval providers, and
builds the OpenAI client. The reference provider is built the first time a
binding names it (at startup, when the gate admits the binding): it opens
its own autocommit Postgres connection to the framework's database, applies
the gate's DDL, and loads the models. A reasoner registered without a plugin
is admitted later, from the asset root (below). No Elasticsearch connection
is opened until a decision retrieves, so a deployment whose decisions never
reach the gate runs with Elasticsearch down.

## What a reasoner sends

A reasoner addresses the gate with a gate context whose `gate_config`
satisfies `PolicyGateConfig` (the gate's `config_model`, validated by the
framework at intake on every route, recorded decisions included). The
example reasoner sends this shape (values illustrative):

```python
GateContext(
    gate_id="policy",
    gate_config={
        "template_id": "example-v2",                 # a template the reasoner ships
        "template_vars": {"widget_score": "0.612", "region": "REGION_A"},  # its placeholders
        "retrieval_query": "widget inspection in REGION_A: ...",
        "jurisdictions": ["REGION_A", "GLOBAL"],     # retrieval filter (both stores)
    },
)
```

`risk_tier` (a filter on the dense store only) is optional. Unknown keys are
rejected (`422 GATE_CONFIG_INVALID`).

Who may send what is decided by the gate, not by the reasoner's
registration. When the gate is built it derives a `ReasonerProfile` for every
composed reasoner plugin from what it ships: the template ids in its
`prompts_dir` and the `jurisdiction` front matter of its `corpus_dir`
documents. The built gate is a `ConfigAuthorizer`: at intake a reasoner may
render only templates it ships and filter only to jurisdictions its own
corpus directory covers (`422 GATE_CONFIG_UNAUTHORIZED` otherwise) — its
own directory, not yet the corpora its binding names, so a reasoner that
binds another's corpus filters only to jurisdictions its own documents
cover.

It is also a `BindingValidator` and a `ReasonerScope` (DR-33). The framework
asks it to admit every registration that binds it, at startup and when a
registration is written over the API. A plugin reasoner is admitted as it
is. A reasoner registered by file or over the API is admitted from the
asset root, `POLICY_RAG_GATE_ASSETS_ROOT`, laid out
`<root>/<reasoner_id>/prompts/*.yaml` and `<root>/<reasoner_id>/corpus/**/*.md`:
the gate reads that directory once, adds its templates to the prompt
registry (template ids are global, so one already registered refuses the
binding) and its profile, and serves it from then on. A missing directory,
no template, a malformed template, or a corpus with no document that parses
refuses the binding. Every binding, a plugin's too, also needs its
configuration: a `PolicyBindingConfig` naming at least one corpus that is
loaded, whose provider is installed and can supply the binding's
`min_provenance`; a binding without one is refused. See
[Registration](../../docs/design/registration.md) and
[Retrieval providers](../../docs/design/gates/policy/providers.md).

It is also an `EvidenceVerifier` (DR-34): when a decision is verified
(`POST /api/v1/decisions/{decision_id}/verify`), it says how each of its
evidence references stands — `intact`, `source_changed`,
`source_unavailable`, or `not_verifiable` — against the document it escrowed
or, with `?live=true`, against the document as its provider holds it now
(the provider's optional `DocumentFetcher`; the reference provider reads
`policy_rag.documents`, never Elasticsearch). Verifying never calls the LLM
or retrieves, and replay never asks the gate anything. See
[Evidence](../../docs/design/gates/policy/evidence.md#replay-and-verify).

## Infrastructure

The gate owns all of it (DR-28; the framework's only infrastructure is
Postgres):

- **Postgres.** The reference provider's three tables live in the gate's
  own `policy_rag` schema inside the framework's database:
  `policy_rag.corpora` (one row per loaded corpus: manifest hash, document
  count, load time), `policy_rag.documents` (every document version, whole),
  and `policy_rag.policy_chunks` (the chunks and their embeddings). The DDL
  in `src/policy_rag_gate/sql/` (vector extension, schema, tables, indexes)
  is idempotent and applied by the reference provider when it is built and
  by the corpus loader before every load; `make prepare-gate-policy_rag`
  (`uv run python -m policy_rag_gate.schema`) applies it on its own. A
  schema from before DR-34 (chunks keyed by `reasoner_id`) is refused, not
  migrated. Its DDL never touches the framework's `decisionledger` schema.
- **Escrow.** Not the gate's: the documents it escrows go into the
  framework's `decisionledger.evidence_artifacts`, through the framework's
  store (`framework.store.artifacts`, which the plugin hands the gate when
  it builds it). `DECISIONLEDGER_EVIDENCE_ESCROW=off` turns escrow off; the
  gate never reads the setting.
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
| `POLICY_RAG_GATE_ASSETS_ROOT` | blank (none) | The prompts and corpus of reasoners registered without a plugin |
| `OPENAI_API_KEY` | none | The gate's model calls; RAGAS in the eval |
| `ANTHROPIC_API_KEY` | none | The eval's judges |

The settings class is `PolicyRagGateSettings` (prefix `POLICY_RAG_GATE_`;
the provider keys keep their providers' names). The Postgres DSN comes from
the framework's settings.

## Loading a corpus

A reasoner's registration names, on its binding to the gate, the corpora
the gate reads for it (`PolicyBindingConfig`: corpus id, provider, tier;
DR-34). The reference provider's corpora are loaded by the gate's loader,
under the corpus id the bindings name, before the deployment that binds
them starts or the registration that binds them is written (the gate
refuses a binding to a corpus that is not loaded):

```bash
make build-policy-index-<reasoner_id>   # e.g. make build-policy-index-ato-reasoner (as ato-policy)
# same as:
uv run python -m policy_rag_gate.corpus_loader --corpus-id <corpus_id> --reasoner-id <reasoner_id>
```

The loader reads the documents from the composed reasoner plugin with that
`reasoner_id`, else `<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus`
(`--corpus-dir` overrides both), chunks and embeds every Markdown document,
and replaces the corpus in both stores: its `policy_rag.corpora` row
(manifest hash, document count), its documents whole, and its chunks under
deterministic ids (a reload overwrites by id, and the corpus's chunks the
load did not write are deleted). `--wipe` first deletes the corpus's
Elasticsearch documents. It asks no registry. Because a load replaces the
corpus, a file that does not parse refuses the whole load instead of
dropping its document; the manifest hash covers each document's text and
front matter, and two loads of one corpus never interleave. See
[Policy corpus](../../docs/design/gates/policy/policy-corpus.md) for the
document format and the chunking rules, and
[Retrieval providers](../../docs/design/gates/policy/providers.md) for the
provider seam.

## Eval

The gate owns its eval dimensions (retrieval quality, generation
faithfulness, and citation accuracy, with the LLM judge and the RAGAS
scorer) in `src/policy_rag_gate/eval/`. They run against the golden datasets
a reasoner ships for the gate under its `EvalAdapter.datasets_dir`
(`retrieval/golden_queries.yaml`, `faithfulness/golden_outputs.yaml`,
`citations/golden_outputs.yaml`, and optionally
`authority/golden_outputs.yaml`, captured tiered verdicts for the
authority-violation rate). Retrieval is scored over the reasoner's binding,
overall and per bound corpus:

```bash
make eval-gate-policy_rag                     # every installed reasoner that ships datasets for this gate
make eval-gate-policy_rag REASONER=<id>       # one reasoner
uv run python -m policy_rag_gate.eval [--reasoner-id <id>] [--output-dir <dir>]
```

Reports land in `outputs/stage/eval/gate/policy/<reasoner_id>/eval-report.json`.
The run is slow and makes paid LLM calls; it needs `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` (exit 2 without them) and the corpora the reasoner's
binding names loaded.
See [Evaluation](../../docs/design/evaluation.md) for the thresholds.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest`. Unit tests need
no Docker: they use the fixture prompt and corpus under `tests/fixtures/`
and fake stores, and import nothing from the framework's `tests/` tree.
`tests/integration/` needs the stack (`docker compose up -d`) and loads the
fixture corpus under its own corpus ids, cleaning up after itself. The
provider seam is unit-tested with an in-memory provider (`FakeProvider` in
`tests/conftest.py`). Per
project rules the LLM path is not unit-tested with mocked responses; the
eval harness is its test suite.
