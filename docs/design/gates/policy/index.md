# Policy gate

The **RAG policy gate** is the reference LLM gate. It is the workspace
member `gates/policy_rag` (distribution `decisionledger-gate-policy-rag`,
package `policy_rag_gate`, `gate_id` `policy`, `kind` `llm`,
`deterministic=False`), published through the `decisionledger.gates` entry
point `policy = "policy_rag_gate.plugin:PLUGIN"`. The framework never imports
it ([DR-28](../../decisions.md#dr-28-gate-contract-and-gate-plugins)).

For each decision addressed to it, the gate retrieves policy passages from
the submitting reasoner's corpus, renders that reasoner's versioned prompt
template with them, asks an LLM for a structured verdict, and returns the
universal `GateOutput`. It is shared by every reasoner that binds it and
contains no domain wording: the prompt and the corpus are the reasoner's.
Both shipped reasoners bind it as an advisory gate, so it runs only on the
decisions they ask the ledger to gate.

## Configuration

The gate's `config_model` is `PolicyGateConfig`
(`policy_rag_gate/config.py`). The ledger validates every context addressed
to the gate against it at intake, on recorded decisions too. It is lax, so a
reasoner's plain JSON validates as it arrives, and it rejects unknown keys.

| Field | Default | Meaning |
|---|---|---|
| `template_id` | required | The prompt template to render; the audit key recorded in the gate's snapshot. |
| `template_vars` | `{}` | Substitutions for the template's placeholders. |
| `retrieval_query` | `None` | The query the reasoner rendered. `None` or blank makes the gate fail closed before it retrieves. |
| `jurisdictions` | `[]` | Retrieval filter in both stores; empty means no filter. |
| `risk_tier` | `None` | Retrieval filter in the dense store only (a chunk with no tier matches every tier). |
| `corpus_version` | `"unknown"` | The reasoner's corpus version label, recorded in the snapshot. |

The example reasoner (`reasoners/example`, `example_reasoner/assembler.py`)
sends a context of this shape; the values are illustrative:

```json
{
  "gate_id": "policy",
  "gate_config": {
    "template_id": "example-v1",
    "template_vars": {
      "widget_score": "0.612",
      "region": "REGION_A",
      "wobble": "0.41",
      "misalignment_mm": "1.8",
      "prior_inspections": "2",
      "thin_history": "True"
    },
    "retrieval_query": "widget inspection in REGION_A: wobble 0.41, gizmo misalignment 1.8 mm, 2 prior inspections; release criteria, calibration review, sprocket inspection, escalation",
    "jurisdictions": ["REGION_A", "GLOBAL"],
    "corpus_version": "example-v1"
  }
}
```

`GET /api/v1/gates` publishes the schema as the gate's `config_schema`.

### Who may send what

The gate, not the reasoner's registration, decides what each reasoner may
send. When it is built, it derives a `ReasonerProfile` for every composed
reasoner plugin from what that reasoner ships: the template ids in its
`prompts_dir`, and the `jurisdiction` front matter of the documents in its
`corpus_dir` (a document whose front matter does not parse is skipped, as
the loader skips it). The built `PolicyGate` is a `ConfigAuthorizer`: at
intake it accepts only a `template_id` the submitter ships and
`jurisdictions` its own corpus covers, and anything else is `422
GATE_CONFIG_UNAUTHORIZED`.

It is also a `ReasonerScope`: it serves only the reasoners it built a profile
for. A reasoner registered through the deployment's registration file
(`DECISIONLEDGER_REGISTRATIONS`) has no plugin installed, so no prompts or
corpus, and startup refuses a deployment in which such a reasoner binds this
gate ([DR-31](../../decisions.md#dr-31-wire-submission-schema-http-surface-registration-as-data)).

## How it decides

`PolicyGate.evaluate` (`policy_rag_gate/gate.py`):

1. **Template.** Parse the configuration and look up `template_id` in the
   prompt registry. An unknown template raises, and the ledger records the
   gate as failed (HOLD).
2. **Query.** With no `retrieval_query`, return `verdict=None` (failure
   `missing_retrieval_query`, retrieval path `skipped`).
3. **Retrieve** (`PolicyRetriever.retrieve`), scoped to the submitting
   reasoner (`reasoner_id` filters both stores, for tenant isolation):
    - **Dense:** pgvector cosine similarity (HNSW index) over the gate's
      chunk table, embedding the query with `all-MiniLM-L6-v2`; filtered by
      `reasoner_id`, `jurisdictions`, and `risk_tier`.
    - **Sparse:** Elasticsearch BM25 over the `policy-chunks` index, querying
      both `text` (English analyzer) and `text.keyword_search` (standard
      analyzer, for exact identifiers; DR-13); filtered by `reasoner_id` and
      `jurisdictions`.
    - **Fuse:** reciprocal rank fusion (k = 60) of the two lists, ten
      candidates from each for the five snippets the gate keeps.
    - **Rerank:** the `cross-encoder/ms-marco-MiniLM-L-6-v2` cross-encoder
      reorders the fused candidates, under a 200 ms latency budget. When
      scoring takes longer (measured after it returns), the gate keeps the
      RRF order. The path taken, `reranked` or `rrf_only`, is recorded.

   An error from either store raises, and the ledger records the gate as
   failed (HOLD).
4. **Render.** Replace `{policy_snippets}` in the template with the numbered
   snippets, then substitute `template_vars`. A placeholder with no value
   returns `verdict=None` (failure `render_error`).
5. **Ask the model.** Call the OpenAI adapter (`policy_rag_gate/llm/`;
   `gpt-4o-2024-08-06` by default, 30 s per request) with the template's
   system text as the system message and strict structured output into
   `PolicyGateVerdict`. Any error, refusal, or schema failure returns
   `verdict=None` (failure `llm_error`).

Retrieval runs synchronously inside `evaluate`, which blocks the event loop
for its duration; moving it to a thread is a gate-owned follow-up (DR-29).

## What it returns

| `GateOutput` field | Content |
|---|---|
| `verdict` | The universal `GateVerdict` part of the model's `PolicyGateVerdict` (permitted actions, required controls, confidence, escalation), or `None`. |
| `evidence` | One `EvidenceRef` per retrieved snippet: `source_id` the document's `policy_id`, `locator` its section path, `jurisdictions` the snippet's jurisdiction. |
| `evidence_snapshot` | A `PolicyGateSnapshot`: the retrieval (`retrieval_query`, `retrieval_results`, `retrieval_path`, `index_version`) and the exact prompt (`model_version`, `prompt_template_id`, `prompt_template_version`, `corpus_version`, `rendered_prompt`, the `prompt_snapshot` of the template's text and system text, `template_vars`). |
| `extra` | A `PolicyGateExtra`: `rationale`, `citations`, `response_text`, `token_cost`, `retrieval_ms`, and `failure` (the stage that failed, when there is no verdict). |

Because the gate is non-deterministic, enforcement applies every gate-level
tier to it ([How to implement a gate](../implementation.md#enforcement-of-one-gate)).
The jurisdiction-conflict tier reads the jurisdictions of the retrieved
snippets, so it sees exactly the retrieved set, and checks them against the
submitting reasoner's `jurisdiction_conflict_groups`; when retrieval returns
nothing, the tier is skipped.

Everything the gate consulted is in the bundle, so replay re-enforces the
stored output without the LLM, the corpus, or either index. The plugin does
not set an index version, so `index_version` records `"unknown"`.

## Prompts belong to the reasoner

Each reasoner ships its prompt templates in its plugin's `prompts_dir`, and
the gate's `YamlPromptRegistry` loads one directory per composed reasoner
when it is built. A template YAML carries `template_id` (the file name's stem
when absent), `version`, `description`, `template`, and an optional top-level
`system:` key with the
system message sent alongside the rendered template. A template without
`system:` gets the generic `DEFAULT_SYSTEM_TEXT`
(`policy_rag_gate/prompt_registry.py`); an empty `system:` is an error. The
template's placeholders other than `{policy_snippets}` (which the gate fills)
are its required variables.

Template ids are global: the same `template_id` in two reasoners'
directories fails the gate's build, because the id is the audit key in every
decision the gate makes. Template files are immutable: a change is a new
file with a new version, and the reasoner selects it by sending the new
`template_id`. For example, the ATO reasoner ships `ato-v1` to `ato-v3`
(`reasoners/account_takeover/src/ato_reasoner/prompts/`) and sends `ato-v3`,
which moved ATO's former hardcoded system prompt into a `system:` key
([DR-26](../../decisions.md#dr-26-reasoner-packaging-and-discovery)). The
example reasoner ships `example-v1`.

## Infrastructure

The gate owns all of it; the framework's only infrastructure is Postgres.

- **Postgres.** The chunk table `policy_rag.policy_chunks` lives in the
  gate's own `policy_rag` schema inside the framework's database. The DDL in
  `policy_rag_gate/sql/` creates the `vector` extension, the schema, the
  table (`embedding vector(384)`, matching the embedding model), and its
  indexes, idempotently. The plugin applies it on every build, over its own
  autocommit connection, before registering the pgvector adapters; the corpus
  loader applies it before every load; `make prepare-gate-policy_rag`
  (`uv run python -m policy_rag_gate.schema`) applies it alone. The root
  compose file's Postgres image (`pgvector/pgvector:pg16`) ships the
  extension.
- **Elasticsearch.** The service is defined in `gates/policy_rag/compose.yaml`,
  which the root `docker-compose.yml` includes, published on
  `POLICY_RAG_GATE_ELASTICSEARCH_PORT` (default 9200). The gate reaches it at
  `POLICY_RAG_GATE_ELASTICSEARCH_URL` (default `http://localhost:9200`). The
  index mapping is `policy_rag_gate/es/policy-chunks.json`. Building the gate
  opens no Elasticsearch connection, so a deployment whose decisions never
  reach the gate runs with Elasticsearch down.
- **Models.** The `all-MiniLM-L6-v2` sentence-transformer and the
  `cross-encoder/ms-marco-MiniLM-L-6-v2` cross-encoder, downloaded on first
  use.
- **LLM.** The OpenAI adapter for the gate (`OPENAI_API_KEY`); the Anthropic
  adapter for its eval judges (`ANTHROPIC_API_KEY`). The gate's `llm/`
  package is the only place in the repository an LLM SDK is imported.

## Indexing a corpus

A reasoner's corpus reaches the gate's stores through the gate's loader:

```bash
make build-policy-index-<reasoner_id>     # e.g. make build-policy-index-ato-reasoner
uv run python -m policy_rag_gate.corpus_loader --reasoner-id <reasoner_id> [--corpus-dir <path>] [--wipe]
```

The loader resolves the corpus directory from the installed reasoner plugin
with that `reasoner_id` (`--corpus-dir` overrides it), chunks and embeds
every Markdown document, and writes both stores, tagging every chunk with
the `reasoner_id`. See [Policy corpus](policy-corpus.md) for the document
format, the chunking rules, and the options.

## Eval

The gate's eval dimensions (retrieval quality, generation faithfulness,
citation accuracy) live in `policy_rag_gate.eval` and run against the
datasets each reasoner ships for the gate: `make eval-gate-policy_rag`
(`REASONER=<id>` narrows it). See
[Evaluation](../../evaluation.md#the-rag-policy-gates-harness).

## In this section

- **[Policy corpus](policy-corpus.md)**: what a corpus document looks like,
  how the loader chunks and indexes it, and the ATO reasoner's 32-document
  reference corpus.
