# Evidence

The bundle is the evidence; a corpus is only a source. Corpora change, no
retrieval platform versions its corpus, and retrieval and generation cannot
be reproduced later. So the RAG policy gate keeps, in every bundle it
decides, by value what the model saw and by reference how it was produced,
and it escrows in the ledger the whole document each passage came from, so
that what the decision relied on can be proven later, whatever becomes of
the corpus
([DR-34](../../decisions.md#dr-34-retrieval-providers-corpus-references-the-evidence-contract-and-evidence-escrow)).
Replay re-enforces the stored output and never touches a provider; verify,
a separate operation, checks the evidence ([below](#replay-and-verify)).

## The evidence contract

Every passage a provider returns is a `RetrievedContext`
(`policy_rag_gate/models/context.py`), strict and frozen; the provider seam
accepts nothing else.

| Field | Meaning |
|---|---|
| `corpus_id`, `provider`, `resource`, `tier` | Where the passage came from, as the binding names it. |
| `document_id`, `title`, `version` | Its document, and the version when the provider knows it. |
| `document_uri` | Where the document lives, when the provider gives one (the reference provider keeps the document itself). |
| `jurisdiction` | The document's jurisdiction tag, when it has one (enforcement's jurisdiction-conflict tier reads it). |
| `section_path`, `span` | Where in the document the passage sits; `span` is `(start, end)` in the stored document text, when the passage is a contiguous slice of it. |
| `text` | The passage exactly as retrieved. |
| `content_hash` | SHA-256 (lowercase hex) of `text`; the model refuses a digest that does not match. |
| `prompted_text` | The text the prompt carried, only if the gate transformed it (the reference gate never does). |
| `rank`, `scores` | Its position in its corpus's ranking (from 1) and the provider's scores, named; never compared across corpora. |
| `provider_chunk_id` | The provider's own id for the passage. |
| `provenance_level` | `pinned_version`, `uri_and_hash`, or `text_only`. |
| `document_metadata` | Opaque document metadata: the reference provider returns the document's front matter, which carries any lineage its owner put there (an approver, an effective date). |
| `artifact_hash` | The SHA-256 of the whole document the gate escrowed for the passage when it decided (its text as the provider's `DocumentFetcher` returned it, UTF-8), as the ledger's evidence escrow returned it; `None` when nothing was escrowed (escrow off, a provider that cannot fetch documents, a gate built without the escrow). |

The gate also checks each answer against the reference it asked about: the
same corpus, provider, and tier on every passage, at most the quota, ranks
1..n, no passage below the binding's `min_provenance`, and every passage a
`RetrievedContext` whose `content_hash` is the SHA-256 of its text (checked
again, since a result built without validation would skip the model's own
check). A provider that breaks any of it fails the gate closed, and the
decision is held.

The reference provider adds a guarantee of its own: Postgres is
authoritative, so a passage reaches the prompt only if
`policy_rag.policy_chunks` holds that chunk with that exact text. An
Elasticsearch document a failed or interrupted load left behind is dropped
(and logged), so every `pinned_version` passage is one the provider holds
whole.

## What the bundle keeps

The gate's `evidence_snapshot` is a `PolicyGateSnapshot`, version 2
(`policy_rag_gate/models/snapshot.py`):

| Field | Content |
|---|---|
| `snapshot_version` | `2`. |
| `retrieval_query` | The query the reasoner rendered. |
| `corpus_queries` | One `CorpusQuery` per bound corpus, in binding order: the `CorpusRef` (its jurisdictions serialized sorted), the `CorpusState` its provider reported (state marker — the reference provider's covers every document's text and front matter — provenance, provider version), the quota `k`, the `filters` it was queried with, `ranking_requested` and `ranking_applied` (the reference provider's reranker bypass shows here, per corpus), `duration_ms`, how many passages it `returned`, how many were `merged`, and why it was `skipped` when its jurisdictions and the submission's filter do not meet. |
| `contexts` | The passages the model saw, in rendered order. |
| `evidence_layout` | `numbered_list` or `tiered_blocks` (the template's). |
| `dedup_merged` | Passages retrieved but merged into an earlier one with the same text under the same jurisdiction. |
| `index_version`, `model_version` | Labels of the index and the LLM. |
| `prompt_template_id`, `prompt_template_version`, `prompt_snapshot`, `template_vars`, `rendered_prompt` | The exact prompt: the template's text and system text, the substitutions, and the rendered text. |

With the snapshot, an auditor can rebuild `rendered_prompt` without the
prompt registry, a provider, or a corpus. A bundle decided before DR-34
keeps the version-1 shape (`retrieval_results`, one `retrieval_path`, the
submitted `corpus_version`); replay never reads it.

The gate's `evidence` is one universal `EvidenceRef` per rendered passage,
in rendered order: `source_id` the document, `locator` the section,
`jurisdictions` the passage's jurisdiction, `content_hash` the SHA-256 of
its text (so anyone holding the text can prove it is what the decision
relied on), and `artifact_hash` the digest of the document escrowed for it.
The reference at position *n* is the passage at position *n* of
`contexts`; verify matches them that way.

## Escrow

Right after retrieval, before the snapshot or any failure output is built,
the gate fetches the document each passage came from — each
`(corpus, document, version)` once per decision, from the provider of the
corpus query that retrieved it (`DocumentFetcher.fetch_document`; the
reference provider reads `policy_rag.documents`, never Elasticsearch) — and
hands its text (UTF-8, `text/markdown`) to the ledger's evidence escrow.
Every passage from that document then cites the digest the escrow returned
as its `artifact_hash`. The escrow is the framework's
(`decisionledger.evidence_artifacts`, content-addressed by SHA-256, one row
per distinct document however many decisions cite it); the gate's plugin
takes it from the framework's store when the gate is built, and the gate
writes through it from a worker thread.

- **Escrow off** (`DECISIONLEDGER_EVIDENCE_ESCROW=off`): the escrow stores
  nothing, so the passages cite no artifact, and the gate fetches nothing
  (it asks the escrow store it was handed whether it keeps anything; it
  never reads the setting). It records only what the escrow returned.
- **A provider without `DocumentFetcher`**: its passages cite no artifact.
- **A fetch or escrow that fails** fails the gate closed, as any retrieval
  error does: the decision is held, not decided on evidence that cannot be
  kept. A fetched document that is not the one asked for (another id or
  version, or a digest that is not its text's) is a provider contract error
  (`ProviderContractError`). One that no longer holds a passage cited to it
  (the document was reloaded under the same version while the decision was
  made) is refused with `EvidenceEscrowError`, and the decision is held: the
  escrow could never prove that passage.

## Precedence and merging

The gate queries every bound corpus for the same quota and keeps the
passages in precedence order: tier, highest first; then the order the
binding names the corpora; then rank within the corpus. Passages that agree
word for word (the same `content_hash`) under the same jurisdiction are
rendered once, under the higher-precedence corpus, and the merge is counted
on the lower one's `CorpusQuery`. The same words under another jurisdiction
are other evidence and are rendered again: enforcement's
jurisdiction-conflict tier reads every rendered passage's jurisdiction, so
merging them away could hide a conflict.

## Layouts

A template declares how its `{policy_snippets}` is rendered
(`evidence_layout`):

- **`numbered_list`** (the default): one numbered list, each passage as
  `[n] <title> (v<version>) — <jurisdiction>`, its section, then its text —
  byte for byte the rendering every template written before DR-34 got, with
  the same response schema (`PolicyGateVerdict`), so their behavior is
  unchanged.
- **`tiered_blocks`**: blocks by authority tier, highest first, each opened
  by `--- Tier <n> (corpora: …) ---`, each passage numbered across blocks and
  labeled `Corpus: … | Tier: … | Document: … | Version: …` with its section.
  The labels are the only text the gate adds; what the tiers mean is the
  template's to say.

## The conflict/gap protocol

A template with tiered evidence (`ato-v4`, `example-v2`) asks for a
`TieredPolicyGateVerdict`: the policy verdict plus a report on the evidence
— `governing_corpus_id` (the highest-tier corpus with a passage on point),
`conflict_detected` (passages on point disagree), and `evidence_sufficient`
(the passages cover the case). Models adopt wrong retrieved text often and
recognize conflicts rarely, so the gate does not leave the consequence to
the model: when the report says the passages disagree or fall short, the
gate floors its verdict at HOLD and escalates to a human. Of the actions
the model permitted, those at least as restrictive as HOLD are kept and
HOLD replaces the milder ones, so a model BLOCK stays a BLOCK (enforcement
takes the most conservative permitted action): holding never makes a
decision milder. The escalation reason is the gate's own constant text,
with nothing the model wrote in it (`protocol_hold` in `extra` says which
case it was, and the corpus the model named governing stays in `extra`),
and the model's own verdict is kept as `verdict_as_returned`. No
enforcement tier was added: enforcement accepts the gate's verdict, and
its other tiers apply as before — an adversarial probe still blocks, and a
hold whose model confidence is below enforcement's threshold is credited
to the low-confidence tier, which holds the decision as it holds any
low-confidence HOLD or BLOCK.

The gate's eval measures how often a verdict names a lower-tier corpus as
governing while a higher-tier passage on point was retrieved (the
authority-violation rate, with the citation dimension), and reports
retrieval metrics per corpus.

## Replay and verify

**Replay** (`POST /api/v1/decisions/{decision_id}/replay`) re-enforces the
stored output under the bundle's registration snapshot. It never calls a
gate, a provider, or the LLM, and reads no corpus: it answers whether the
recorded action follows from what was recorded.

**Verify** (`POST /api/v1/decisions/{decision_id}/verify`, with
`?live=true` to read the sources as they are now) answers whether what the
decision relied on is still provable. It decides nothing again and changes
nothing. For every evidence reference of every gate that ran it reports two
independent things:

- `escrow`, the framework's own check: whether the escrow holds the
  artifact the reference names (`present`, `missing`), or `none` when the
  reference names none.
- `status`, the gate's answer (`EvidenceVerifier.verify`): how the
  reference stands.

The policy gate matches each reference to its passage by position in the
snapshot's `contexts`; a reference whose `content_hash` is not that
passage's, or any reference of a version-1 snapshot, is `not_verifiable`.
It then checks the passage against a document's text — `intact` when the
document holds it: with a `span`, the text at those offsets hashes to the
passage's `content_hash`; without one (the loader merged a short section
into the next and dropped the heading between them, so the passage is not
contiguous in the document), each of its paragraphs occurs in the
document, in order:

| Mode | Checked against | `intact` | `source_changed` | `source_unavailable` | `not_verifiable` |
|---|---|---|---|---|---|
| stored (default) | the escrowed document | the escrowed text holds the passage | it does not (escrow refuses such a document when deciding, so only an altered record gets here) | — | nothing escrowed, the escrow no longer holds it, or its bytes are not what was kept |
| live | the document as the provider holds it now | the current text holds the passage | the text changed, or the corpus is held but no longer that version | the corpus is not loaded, the provider is not installed or fails, or it answers with another document | the provider cannot fetch documents, or the snapshot has no query for the passage's corpus |

Stored verify reads only the escrow, so it still proves a decision's
passages after the corpus was reloaded, edited, or deleted; live verify
says whether the source still says the same. Each document is read once
per call; neither mode calls the LLM or retrieves. With escrow off, stored
verify can prove nothing for decisions made then (`not_verifiable`), and
live verify still compares with the source.

For example, after a cited document is edited without a new version and
reloaded, live verify reports its passages `source_changed` (its spans no
longer hash to the passages) while stored verify reports them `intact`
from the escrow; after the corpus is deleted from `policy_rag`, live verify
reports `source_unavailable` and stored verify still `intact`.
