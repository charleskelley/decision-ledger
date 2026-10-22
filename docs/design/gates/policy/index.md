# Policy gate

The **RAG policy gate** is the reference LLM gate. It is the workspace
member `gates/policy_rag` (distribution `decisionledger-gate-policy-rag`,
package `policy_rag_gate`, `gate_id` `policy`, `kind` `llm`,
`deterministic=False`), published through the `decisionledger.gates` entry
point `policy = "policy_rag_gate.plugin:PLUGIN"`. The framework never imports
it ([DR-28](../../decisions.md#dr-28-gate-contract-and-gate-plugins)).

For each decision addressed to it, the gate retrieves policy passages from
every corpus the submitting reasoner's registration binds it to, through the
retrieval provider that holds each corpus, renders that reasoner's versioned
prompt template with them, asks an LLM for a structured verdict, and returns
the universal `GateOutput`. It is shared by every reasoner that binds it and
contains no domain wording: the prompt is the reasoner's, and the corpora are
references its binding names
([DR-34](../../decisions.md#dr-34-retrieval-providers-corpus-references-the-evidence-contract-and-evidence-escrow)).
Both shipped reasoners bind it as an advisory gate, so it runs only on the
decisions they ask the ledger to gate.

## Configuration

The gate is configured twice: once per reasoner, on the reasoner's binding
(which corpora to read), and once per decision, in the submission's gate
context (which template, which query, which filters).

### Per reasoner: the binding

The plugin's `binding_config_model` is `PolicyBindingConfig`
(`policy_rag_gate/corpus.py`). A reasoner's registration names, on its
binding to the gate, the corpora the gate retrieves from for it, each a
`CorpusRef`:

| Field | Default | Meaning |
|---|---|---|
| `corpora[].corpus_id` | required | The corpus, as its provider knows it (the reference provider: the `--corpus-id` it was loaded under). |
| `corpora[].provider` | `"reference"` | The retrieval provider that holds it ([Retrieval providers](providers.md)). |
| `corpora[].resource` | `None` | The provider's own name for the corpus, when it needs one. The reference provider takes none. |
| `corpora[].tier` | required | Authority tier, an integer from 1 (the highest). |
| `corpora[].jurisdictions` | `[]` | The jurisdictions the binding reads from this corpus; empty means no filter of its own. |
| `corpora[].min_provenance` | `"text_only"` | The weakest provenance the gate may record for the corpus's passages: `text_only`, `uri_and_hash`, or `pinned_version`. |
| `k_per_corpus` | `5` | Passages retrieved from each corpus per decision (1 to 50). |

At least one corpus, each named once. The ATO reasoner binds `ato-policy`,
and the example reasoner `example-policy`, each at tier 1 with
`min_provenance: pinned_version`:

```yaml
gates:
- gate_id: policy
  mandatory: false
  config:
    corpora:
    - {corpus_id: example-policy, provider: reference, tier: 1, min_provenance: pinned_version}
    k_per_corpus: 5
```

The binding's configuration is part of the registration, so it is versioned
and snapshotted with it, and the framework hands it to the gate with every
request (`GateRequest.binding_config`): a decision always retrieves from the
corpora its registration version names.

### Per decision: the gate context

The gate's `config_model` is `PolicyGateConfig` (`policy_rag_gate/config.py`).
The ledger validates every context addressed to the gate against it at
intake, on recorded decisions too. It is lax, so a reasoner's plain JSON
validates as it arrives, and it rejects unknown keys.

| Field | Default | Meaning |
|---|---|---|
| `template_id` | required | The prompt template to render; the audit key recorded in the gate's snapshot. |
| `template_vars` | `{}` | Substitutions for the template's placeholders. |
| `retrieval_query` | `None` | The query the reasoner rendered. `None` or blank makes the gate fail closed before it retrieves. |
| `jurisdictions` | `[]` | Retrieval filter in both stores of every bound corpus, narrowed per corpus by the binding's jurisdictions for it; empty means no filter. |
| `risk_tier` | `None` | Retrieval filter in the reference provider's dense store only (a chunk with no tier matches every tier). |

A submission no longer labels a corpus version (`corpus_version` was removed
by DR-34; the key is now refused): the gate records the state of every
corpus it queries itself. The example reasoner (`reasoners/example`,
`example_reasoner/assembler.py`) sends a context of this shape; the values
are illustrative:

```json
{
  "gate_id": "policy",
  "gate_config": {
    "template_id": "example-v2",
    "template_vars": {
      "widget_score": "0.612",
      "region": "REGION_A",
      "wobble": "0.41",
      "misalignment_mm": "1.8",
      "prior_inspections": "2",
      "thin_history": "True"
    },
    "retrieval_query": "widget inspection in REGION_A: wobble 0.41, gizmo misalignment 1.8 mm, 2 prior inspections; release criteria, calibration review, sprocket inspection, escalation",
    "jurisdictions": ["REGION_A", "GLOBAL"]
  }
}
```

`GET /api/v1/gates` publishes the schema as the gate's `config_schema`.

### Who may send what

The gate, not the reasoner's registration, decides what each reasoner may
send. When it is built, it derives a `ReasonerProfile` for every composed
reasoner plugin from what that reasoner ships: the template ids in its
`prompts_dir`, and the `jurisdiction` front matter of the documents in its
`corpus_dir` (a document whose front matter does not parse is skipped; the
loader refuses to load a corpus holding one). The built `PolicyGate` is a
`ConfigAuthorizer`: at intake it accepts only a `template_id` the submitter
ships and `jurisdictions` its own corpus directory covers, and anything else
is `422 GATE_CONFIG_UNAUTHORIZED`. That is the reasoner's own directory, not
the corpora its binding names: a reasoner that only shares another's corpus
filters only to the jurisdictions its own documents cover (a limitation
DR-34 records).

It is also a `BindingValidator` and a `ReasonerScope`
([DR-33](../../decisions.md#dr-33-registration-is-a-writable-versioned-resource-bindings-carry-gate-validated-config)).
The framework asks it to admit every registration that binds it, at startup
and when a registration is written over the API. The binding's
configuration must be a `PolicyBindingConfig` (a binding with no
configuration, as every registration written before DR-34 has, is
refused), and every corpus it names must be held by an installed provider
that can describe it (the reference provider: the corpus is loaded) at the
provenance the binding asks for; otherwise the binding is refused, naming
the gate. A plugin reasoner is then
admitted as it is; a reasoner registered by file or over the API is
admitted from the asset root (`POLICY_RAG_GATE_ASSETS_ROOT`,
`<root>/<reasoner_id>/prompts/*.yaml` and `corpus/**/*.md`), read once, its
templates added to the prompt registry and its profile to the gate's. A
reasoner with no assets there, malformed ones, or a template id another
reasoner registers is refused, naming the gate (see
[Registration](../../registration.md)). Load a corpus before the deployment
that binds it starts, or before the registration that binds it is written.

## How it decides

`PolicyGate.evaluate` (`policy_rag_gate/gate.py`):

1. **Template and binding.** Parse the gate context and the request's
   binding configuration, and look up `template_id` in the prompt registry.
   An unknown template raises, and the ledger records the gate as failed
   (HOLD).
2. **Query.** With no `retrieval_query`, return `verdict=None` (failure
   `missing_retrieval_query`; no corpus is queried).
3. **Retrieve** (`retrieve_evidence`): query every bound corpus, in binding
   order, through its provider, for `k_per_corpus` passages, with the
   submission's filters (a corpus whose jurisdictions the submission's
   filter excludes is not queried, and its query is recorded as `skipped`).
   Every answer is held to the evidence contract
   ([Evidence](evidence.md)): a provider that answers for another corpus,
   provider, or tier, returns more than the quota, ranks out of order,
   returns a passage below the binding's `min_provenance`, or returns a
   passage whose `content_hash` is not the SHA-256 of its text fails the
   gate closed (HOLD), as does any provider error. Scores are never
   compared across corpora: the passages are kept in precedence order —
   tier (highest first), then binding order, then rank — and a passage
   whose text a higher-precedence passage already carries under the same
   jurisdiction is merged into it (`content_hash` and jurisdiction
   equality; the same words under another jurisdiction are kept). The
   reference provider's retrieval is the hybrid stack the gate always ran,
   filtered by `corpus_id`, and returns only passages its Postgres holds
   with that exact text
   ([Retrieval providers](providers.md#the-reference-provider)).
4. **Escrow.** Fetch the document each passage came from, once per
   `(corpus, document, version)`, through its provider's `DocumentFetcher`,
   and hand its text to the ledger's evidence escrow (through the
   framework's store, from a worker thread); every passage from it then
   cites the digest the escrow returned (`artifact_hash`; none from a
   provider that cannot fetch documents). With escrow off nothing is
   fetched and no passage cites an artifact. A failed fetch or escrow, or a
   fetched document that no longer holds a passage cited to it
   (`EvidenceEscrowError`), fails the gate closed (HOLD). See
   [Evidence](evidence.md#escrow).
5. **Render.** Replace `{policy_snippets}` in the template with the
   passages, in the layout the template declares (`evidence_layout`): one
   numbered list (the default, exactly as before DR-34) or blocks by
   authority tier with each passage labeled by corpus, tier, document,
   version, and section. Then substitute `template_vars`. A placeholder with
   no value returns `verdict=None` (failure `render_error`).
6. **Ask the model.** Call the OpenAI adapter (`policy_rag_gate/llm/`;
   `gpt-4o-2024-08-06` by default, 30 s per request) with the template's
   system text as the system message and strict structured output into
   `PolicyGateVerdict`, or, for a template with tiered evidence,
   `TieredPolicyGateVerdict`, which adds the conflict/gap report: the
   governing corpus, whether passages on point disagree, and whether the
   evidence covers the case. Any error, refusal, or schema failure returns
   `verdict=None` (failure `llm_error`).
7. **Hold on disagreement or a gap.** When the tiered report says passages
   on point disagree, or the evidence does not cover the case, the gate
   floors its verdict at HOLD — the model's actions at least as restrictive
   as HOLD are kept (a BLOCK stays a BLOCK), HOLD replaces the milder ones —
   and escalates to a human with a constant reason of its own, keeping the
   verdict the model returned in its `extra` (`verdict_as_returned`,
   `protocol_hold`). No enforcement tier was added: enforcement accepts the
   gate's verdict (a low-confidence hold is credited to its low-confidence
   tier, with the same HOLD).

Retrieval runs synchronously inside `evaluate`, which blocks the event loop
for its duration; moving it to a thread is a gate-owned follow-up (DR-29).
The escrow step already runs in a thread.

## What it returns

| `GateOutput` field | Content |
|---|---|
| `verdict` | The universal `GateVerdict` part of the model's verdict (permitted actions, required controls, confidence, escalation) — floored at HOLD, with the gate's escalation, when the tiered report found disagreement or a gap — or `None`. |
| `evidence` | One `EvidenceRef` per rendered passage, in rendered order: `source_id` the document's id, `locator` its section path, `jurisdictions` the passage's jurisdiction, `content_hash` the SHA-256 of its text, and `artifact_hash` the digest of the document escrowed for it. |
| `evidence_snapshot` | A `PolicyGateSnapshot` (version 2): the query, one `CorpusQuery` per bound corpus, the rendered passages (`RetrievedContext`), the layout, the merges, and the exact prompt. See [Evidence](evidence.md). |
| `extra` | A `PolicyGateExtra`: `rationale`, `citations`, `response_text`, `token_cost`, `retrieval_ms`, `failure` (the stage that failed, when there is no verdict), and, for a tiered template, `governing_corpus_id`, `conflict_detected`, `evidence_sufficient`, `protocol_hold`, and `verdict_as_returned`. |

Because the gate is non-deterministic, enforcement applies every gate-level
tier to it ([How to implement a gate](../implementation.md#enforcement-of-one-gate)).
The jurisdiction-conflict tier reads the jurisdictions of the rendered
passages, so it sees exactly the rendered set, and checks them against the
submitting reasoner's `jurisdiction_conflict_groups`; when retrieval returns
nothing, the tier is skipped.

Everything the gate consulted is in the bundle, so replay re-enforces the
stored output without the LLM, a provider, or a corpus. The plugin does not
set an index version, so `index_version` records `"unknown"`; each corpus's
state (the reference provider's manifest hash) is in its `CorpusQuery`.

The gate is also an `EvidenceVerifier`: when a decision is verified
(`POST /api/v1/decisions/{decision_id}/verify`), it says how each of its
references stands — against the document it escrowed, or with
`?live=true` against the document as its provider holds it now — without
calling the LLM or retrieving. See
[Evidence](evidence.md#replay-and-verify).

## Prompts belong to the reasoner

Each reasoner ships its prompt templates in its plugin's `prompts_dir`, and
the gate's `YamlPromptRegistry` loads one directory per composed reasoner
when it is built. A template YAML carries `template_id` (the file name's stem
when absent), `version`, `description`, `template`, an optional top-level
`system:` key with the system message sent alongside the rendered template,
and an optional `evidence_layout:` (`numbered_list`, the default, or
`tiered_blocks`, DR-34). A template without `system:` gets the generic
`DEFAULT_SYSTEM_TEXT` (`policy_rag_gate/prompt_registry.py`); an empty
`system:` is an error. The template's placeholders other than
`{policy_snippets}` (which the gate fills) are its required variables.

Template ids are global: the same `template_id` in two reasoners'
directories fails the gate's build, because the id is the audit key in every
decision the gate makes. Template files are immutable: a change is a new
file with a new version, and the reasoner selects it by sending the new
`template_id`. For example, the ATO reasoner ships `ato-v1` to `ato-v4`
(`reasoners/account_takeover/src/ato_reasoner/prompts/`) and sends `ato-v4`
(the tiered layout with the conflict/gap protocol) unless `ATO_TEMPLATE_ID`
names another; `ato-v3`, which moved ATO's former hardcoded system prompt
into a `system:` key
([DR-26](../../decisions.md#dr-26-reasoner-packaging-and-discovery)),
reproduces the behavior before DR-34 (`ATO_TEMPLATE_ID` accepts `ato-v2`
to `ato-v4`; `ato-v1` is kept for the bundles that cite it but cannot be
rendered again). The example reasoner ships `example-v1` and `example-v2`
and sends `example-v2`.

## Infrastructure

The gate owns all of it; the framework's only infrastructure is Postgres.
Everything below is the reference provider's
([Retrieval providers](providers.md)), built the first time a binding names
it.

- **Postgres.** The `policy_rag` schema inside the framework's database
  holds `corpora` (one row per loaded corpus: its manifest hash, document
  count, load time, and source directory), `documents` (every document
  whole, by version, with its front matter), and `policy_chunks` (one row
  per chunk, keyed by a deterministic `chunk_id` and filtered by
  `corpus_id`; `embedding vector(384)`, matching the embedding model). The
  DDL in `policy_rag_gate/sql/` creates the `vector` extension, the schema,
  the tables, and their indexes, idempotently. The reference provider
  applies it whenever it is built, over its own autocommit connection,
  before registering the pgvector adapters; the corpus loader applies it
  before every load; `make prepare-gate-policy_rag`
  (`uv run python -m policy_rag_gate.schema`) applies it alone. A database
  holding the chunk table from before DR-34 (keyed by `reasoner_id`) is
  refused before anything runs: drop the schema (`docker compose down -v`
  in development) and reload each corpus. The root compose file's Postgres
  image (`pgvector/pgvector:pg16`) ships the extension.
- **Elasticsearch.** The service is defined in `gates/policy_rag/compose.yaml`,
  which the root `docker-compose.yml` includes, published on
  `POLICY_RAG_GATE_ELASTICSEARCH_PORT` (default 9200). The gate reaches it at
  `POLICY_RAG_GATE_ELASTICSEARCH_URL` (default `http://localhost:9200`). The
  index mapping is `policy_rag_gate/es/policy-chunks.json` (`corpus_id` a
  `keyword`; the loader recreates an index mapped otherwise). Building the
  provider opens no Elasticsearch connection, so a deployment whose
  decisions never reach the gate runs with Elasticsearch down.
- **Models.** The `all-MiniLM-L6-v2` sentence-transformer and the
  `cross-encoder/ms-marco-MiniLM-L-6-v2` cross-encoder, downloaded on first
  use.
- **LLM.** The OpenAI adapter for the gate (`OPENAI_API_KEY`); the Anthropic
  adapter for its eval judges (`ANTHROPIC_API_KEY`). The gate's `llm/`
  package is the only place in the repository an LLM SDK is imported.
- **Escrow.** Not the gate's: the documents it escrows live in the
  framework's `decisionledger.evidence_artifacts`, which the gate writes
  through the framework's store (`framework.store.artifacts`, handed to it
  when it is built). `DECISIONLEDGER_EVIDENCE_ESCROW=off` turns escrow off.

## Loading a corpus

A corpus reaches the reference provider's stores through the gate's loader,
under the corpus id the bindings name:

```bash
make build-policy-index-<reasoner_id>     # e.g. make build-policy-index-ato-reasoner (as ato-policy)
uv run python -m policy_rag_gate.corpus_loader --corpus-id <corpus_id> \
    (--reasoner-id <reasoner_id> | --corpus-dir <path>) [--wipe]
```

The loader reads the documents from `--corpus-dir`, or where the gate reads
a reasoner's corpus (`--reasoner-id`): the composed reasoner plugin's
`corpus_dir`, else `<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus`
(DR-33). It chunks and embeds every Markdown document and replaces the
corpus in both stores (deterministic chunk ids: a reload overwrites by id
and deletes what the load did not write); a file that does not parse
refuses the whole load rather than drop its document. It asks no registry:
a corpus is loaded before a registration that binds it is admitted, and
the ledger does not start while a corpus a served registration binds is
not loaded. See
[Policy corpus](policy-corpus.md) for the document format, the chunking
rules, and the options.

## Eval

The gate's eval dimensions (retrieval quality with per-corpus metrics,
generation faithfulness, citation accuracy with the authority-violation
rate) live in `policy_rag_gate.eval` and run against the datasets each
reasoner ships for the gate, over the reasoner's binding as the ledger's
registry composes it: `make eval-gate-policy_rag` (`REASONER=<id>` narrows
it). See [Evaluation](../../evaluation.md#the-rag-policy-gates-harness).

## In this section

- **[Policy corpus](policy-corpus.md)**: what a corpus document looks like,
  how the loader chunks and loads it, and the ATO reasoner's 32-document
  reference corpus.
- **[Retrieval providers](providers.md)**: the provider seam, the reference
  provider, corpus references, and how to add a provider.
- **[Evidence](evidence.md)**: the evidence contract every provider meets,
  the snapshot the gate records, the escrow, the tiered layout, the
  conflict/gap hold, and replay versus verify.
