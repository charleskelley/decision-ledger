# Retrieval providers

The RAG policy gate does not manage corpora. A reasoner's registration
names, on its binding to the gate, the corpora the gate reads for it, and
each corpus is held by a **retrieval provider**: the gate asks each provider
for passages and holds every answer to the evidence contract. The record is
[DR-34](../../decisions.md#dr-34-retrieval-providers-corpus-references-the-evidence-contract-and-evidence-escrow);
the code is `policy_rag_gate/providers/` (the seam and the reference
provider) and `policy_rag_gate/corpus.py` (corpus references and the
binding configuration).

Providers are the gate's plugins, not the framework's: the framework never
learns what a corpus, a provider, or an authority tier is (the boundary test
forbids those words in `core/`, `app/`, and `eval/`), and no reasoner or
integration member's source imports the provider seam.

## Corpus references

A binding's configuration is a `PolicyBindingConfig`: at least one
`CorpusRef`, each named once, and a quota per corpus.

```yaml
config:
  corpora:
  - corpus_id: regulatory          # as the provider knows it
    provider: reference            # the default
    tier: 1                        # 1 is the highest authority
    jurisdictions: [EU]            # optional: what the binding reads from it
    min_provenance: pinned_version # optional; text_only by default
  - corpus_id: practice-notes
    tier: 2
  k_per_corpus: 5
```

- **Tier.** The gate renders evidence grouped by tier, highest first, and
  never compares scores across corpora: research on multi-corpus retrieval
  finds scores incomparable across indexes and routers that pick a corpus
  wrong half the time, so every corpus gets the same quota and its passages
  are ranked within it only. Two corpora may share a tier.
- **Jurisdictions.** The binding's jurisdictions for a corpus narrow the
  submission's jurisdiction filter for that corpus; a corpus the
  submission's filter excludes entirely is not queried (its `CorpusQuery`
  says why).
- **Minimum provenance.** How well the provider must pin a passage down:
  `text_only` (the text alone), `uri_and_hash` (the text plus the URI of its
  document, whose content the gate hashes), or `pinned_version` (the exact
  document version, which the provider keeps and can return whole). The gate
  refuses a binding whose provider cannot supply it, and fails a decision
  whose passages fall below it.

Two reasoners may bind the same corpus, each at its own tier; the corpus is
loaded once. One limit remains: the jurisdictions a reasoner may filter to
at intake are still those its own corpus directory covers (its plugin's
`corpus_dir`, or `corpus/` under the asset root, which an asset-root
reasoner must ship), not those of the corpora its binding names. A reasoner
that only shares another's corpus can therefore filter only to the
jurisdictions its own documents cover; moving authorization to the bound
corpora needs the binding at intake, which the gate's intake check does not
receive
([DR-34](../../decisions.md#dr-34-retrieval-providers-corpus-references-the-evidence-contract-and-evidence-escrow)).

## Admission

When a registration that binds the gate is composed at startup or written
over the API, the gate's `validate_binding` checks, before it changes
anything:

1. the configuration is a valid `PolicyBindingConfig`;
2. every corpus's provider is installed (and can be built);
3. the provider can describe the corpus (`describe` succeeds; the reference
   provider: the corpus is loaded);
4. the provenance the provider supplies meets the binding's
   `min_provenance`.

A refusal fails startup or answers the write `422 GATE_BINDING_REFUSED`,
naming the gate. So the order for a new corpus is: load it, then start the
deployment or write the registration that binds it. Admission keeps no copy
of the binding: the framework hands the gate the binding's configuration
with every request (`GateRequest.binding_config`), from the registration
version the decision is made under.

## The seam

```python
class RetrievalProvider(Protocol):
    provider_id: str
    def describe(self, corpus: CorpusRef) -> CorpusState: ...
    def retrieve(self, query: str, corpus: CorpusRef, *, k: int,
                 jurisdictions: Sequence[str] | None,
                 risk_tier: str | None) -> ProviderResult: ...

class DocumentFetcher(Protocol):  # optional capability
    def fetch_document(self, corpus: CorpusRef, document_id: str,
                       version: str | None) -> DocumentBlob: ...
```

- `describe` reports a `CorpusState`: a state marker (the reference
  provider: the manifest hash of the documents last loaded, over their text
  and front matter), the provenance
  level the provider supplies, its own version, and, when it knows them, the
  document count and when the corpus last changed. No provider versions its
  corpus, so the state is a marker, not a promise that retrieval can be
  reproduced; the gate records it with every query.
- `retrieve` returns a `ProviderResult`: the state, the passages
  (`RetrievedContext`, ranked 1..n within the corpus), and the ranking it
  attempted and applied. `RetrievedContext` is the evidence contract
  ([Evidence](evidence.md)): every passage carries its corpus, tier,
  document, rank, provenance level, and the SHA-256 of its text, which must
  match. The gate refuses anything else (`ProviderContractError`, and the
  decision is held).
- `fetch_document`, an optional capability (`DocumentFetcher`), returns one
  document whole as a `DocumentBlob`: its corpus, id, and version, title,
  `media_type` (`text/markdown`), `text` (the text the passages' spans
  index), the SHA-256 of that text (which must match), and the provider's
  metadata. It raises `DocumentNotHeldError` when the provider holds the
  corpus but not that document version, and anything else when it cannot
  read the document at all (the corpus not held, the store unreachable).
  The gate uses it twice: to escrow, in the ledger, the document each
  passage came from when it decides, and to read the document as it is now
  when a decision is verified live ([Evidence](evidence.md#escrow)). A
  provider without it gets neither: its passages cite no escrowed artifact,
  and live verify reports them `not_verifiable`.

Providers are discovered under the gate's own entry-point group,
`policy_rag_gate.providers`; each entry point names a `ProviderPlugin`
whose `provider_id` equals the entry point's name and whose
`build(ProviderServices)` opens the provider (the services carry the
framework's DSN and the gate's settings). The gate's `ProviderRegistry`
builds a provider the first time a binding names it, once, and closes it
with the gate.

## The reference provider

`reference` (`policy_rag_gate/providers/reference.py`) ships in the member
and is the default. It is the hybrid stack the gate always ran, unchanged,
behind the seam:

- **Dense:** pgvector cosine similarity (HNSW index) over
  `policy_rag.policy_chunks`, embedding the query with `all-MiniLM-L6-v2`,
  filtered by `corpus_id`, `jurisdictions`, and `risk_tier` (a chunk with no
  tier matches every tier).
- **Sparse:** Elasticsearch BM25 over the `policy-chunks` index, querying
  `text` (English analyzer) and `text.keyword_search` (standard analyzer, for
  exact identifiers; DR-13), filtered by `corpus_id` and `jurisdictions`.
- **Fuse:** reciprocal rank fusion (k = 60) of the two lists, twice the quota
  from each, keyed on the chunk id (a chunk found by both legs adds up its
  two ranks).
- **Rerank:** `cross-encoder/ms-marco-MiniLM-L-6-v2` under a 200 ms latency
  budget; when scoring takes longer, the RRF order stands
  (`ranking_applied: rrf_only`, `ranking_requested: reranked`). A query that
  finds nothing reports the ranking requested as applied: nothing was
  bypassed.
- **Check:** a hit is kept only if Postgres holds that chunk id with that
  exact text; an Elasticsearch document a failed or interrupted load left
  behind is dropped (logged as `reference_provider.unheld_hits_dropped`),
  and the ranks close up.

`corpus_id` filters both stores, so a query never reaches another corpus:
which corpora a reasoner reads is its binding, not a key on the chunks. The
provider holds every corpus whole (`policy_rag.documents`, by document and
version), so its passages carry `pinned_version` provenance; each passage's
`document_metadata` is its document's front matter, and its
`provider_chunk_id` the chunk's deterministic id. It takes no `resource`.
A corpus it does not hold cannot be described, so a binding to it is
refused at admission.

It is a `DocumentFetcher`: `fetch_document` returns one document version
whole from `policy_rag.documents` (its `body`, front matter stripped, and
its front matter as `metadata`), in one statement over the provider's own
Postgres connection; Elasticsearch is never involved. The version is
required, since a corpus may hold several versions of one document. A
corpus that is not loaded raises `CorpusNotLoadedError`; a loaded corpus
without that version (removed or re-versioned by a reload) raises
`DocumentNotHeldError`.

### Loading a corpus

```bash
uv run python -m policy_rag_gate.corpus_loader --corpus-id <corpus_id> \
    (--reasoner-id <reasoner_id> | --corpus-dir <path>) [--wipe]
```

The loader replaces one corpus: in one Postgres transaction it upserts the
corpus's row in `policy_rag.corpora` (manifest hash, document count, load
time, source directory) and replaces its documents and chunks; in
Elasticsearch it writes every chunk under its id and deletes the corpus's
chunks the load did not write. Chunk ids are deterministic,
`sha256(corpus_id, document_id, version, section_path)` (plus the
occurrence of a section path a document repeats), so a reload overwrites
instead of duplicating. The manifest hash, the provider's state marker,
covers every document's id, version, text, and front matter. A file that
does not parse, or two files declaring one document version, refuse the
load, and two loads of one corpus never interleave (an advisory lock on the
corpus id covers both stores). See [Policy corpus](policy-corpus.md#loading).

## Adding a provider

A provider for a managed retrieval service (Vertex AI RAG Engine, Bedrock
Knowledge Bases) is a `ProviderPlugin` published under
`policy_rag_gate.providers` from any installed distribution:

```toml
[project.entry-points."policy_rag_gate.providers"]
vertex = "my_vertex_provider:PLUGIN"
```

It names corpora by `resource` (a corpus resource name), reports its own
state marker (for example the corpus's update time), and declares the
provenance it can supply (a provider that returns document URIs and text
supplies `uri_and_hash`). A binding then names it per corpus:
`{corpus_id: regulatory, provider: vertex, resource: projects/…/ragCorpora/…, tier: 1}`.
No framework or reasoner code changes. To have its passages' documents
escrowed and checked live, it also implements `fetch_document`; this
repository ships that only for `reference`.
