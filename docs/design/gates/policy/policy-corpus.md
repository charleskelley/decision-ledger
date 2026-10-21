# Policy corpus

A **policy corpus** is the set of Markdown policy documents the
[RAG policy gate](index.md) retrieves from when it decides for a reasoner.
The corpus belongs to the reasoner: it ships in the reasoner's package, in
the directory its plugin exposes as `corpus_dir`, and is versioned with it.
The gate owns the stores: its corpus loader chunks, embeds, and indexes a
reasoner's corpus into the gate's Postgres table and Elasticsearch index,
tagged with that reasoner's `reasoner_id`, and its retriever reads only the
indexed rows, never the files. Updating a corpus (a new document, a
corrected threshold) is a reasoner change plus a re-index, never a framework
or gate change.

The first half of this page is the format any reasoner's corpus follows. The
second half describes the ATO reasoner's reference corpus, which is ATO's
own content.

## The two-layer citation model

A grounded policy decision cites two kinds of source, and a corpus is
designed to supply both (the examples are from ATO's corpus):

- **Layer 1, the what: internal policy documents.** Specific, actionable
  rules with concrete thresholds (travel speed above 900 km/h is impossible
  travel, and "BLOCK is mandatory with no exception and no override"). The model
  cites these to explain which action applies.
- **Layer 2, the why: regulation and guidance.** The external justification
  for those rules ("FFIEC guidance requires institutions to assess anomalous
  geographic access patterns"). The model cites these to ground the decision
  in an authority.

Internal policies operationalize regulatory requirements. Citing only
internal policy produces circular reasoning; citing only regulation produces
reasoning with no operational specificity. The gate's citation dimension
([Evaluation](../../evaluation.md#the-rag-policy-gates-harness)) has an LLM
judge rate whether each cited snippet supports the claim it is cited for.

## Document format

Each document is one Markdown file with YAML front matter (this one is from
ATO's corpus):

```markdown
---
policy_id: INT-GEO-RISK-V1
title: Geographic Risk Controls Policy
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2024-03-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

## 1. Purpose and Scope

This policy establishes geographic risk controls for login event evaluation...

## 2. Regulatory Basis

...
```

The front matter validates as the gate's `PolicyDocument`
(`policy_rag_gate/models/corpus.py`):

| Field | Type | Meaning |
|---|---|---|
| `policy_id` | string | Stable document id. Becomes every chunk's `policy_id`, the snippet's `document_id`, and the `source_id` of the gate's evidence. |
| `title` | string | Human-readable title, stored on every chunk so the retriever can cite without reading the corpus. |
| `version` | string | The document's version. Quote it (`"1.0"`) so YAML keeps it a string. |
| `jurisdiction` | string | The regulatory jurisdiction, in the reasoner's own vocabulary. A retrieval filter, the jurisdiction tag on the gate's evidence, and part of what the reasoner may filter to (its profile). |
| `effective_date` | date | When this version became authoritative. |
| `supersedes` | string or `null` | The `policy_id` this document replaces. |
| `risk_tier` | string or `null` | The subject tier the document applies to; `null` applies to every tier. A dense-store retrieval filter. |
| `document_type` | enum | `REGULATION`, `GUIDANCE`, `STANDARD`, or `INTERNAL_POLICY`. |

`jurisdiction` and `risk_tier` are plain strings, so each reasoner uses its
own classification vocabulary and the gate imports none of it. The loader
finds documents recursively under `corpus_dir` (`*.md`); how a reasoner
arranges subdirectories is its choice. A document whose front matter is
missing, malformed, or invalid is skipped, both by the loader (which logs a
warning) and when the gate derives the reasoner's profile, so a skipped document's
jurisdiction is never one the reasoner may filter to.

## Chunking

The loader (`policy_rag_gate/corpus_loader.py`) chunks each body at section
headings; it uses word counts and needs no tokenizer:

- H1 title lines are dropped (they are document-level, not chunk content).
- The primary split is at `##` headings. Each chunk's `section_path` is the
  heading text (`"3. Impossible Travel Detection and Mandatory BLOCK"`).
- A `##` section longer than 300 words is split at its `###` headings
  instead, with a compound `section_path` (`"4. Action Decision Matrix > 4.2
  CHALLENGE Criteria"`), so no two chunks of a document share a path. Text
  between the `##` heading and the first `###` becomes its own chunk under
  the `##` path when it has at least 30 words, and is dropped otherwise.
- Text before the first `##` is indexed as `"Overview"` when it has at
  least 30 words, and is dropped otherwise.
- A chunk shorter than 30 words is merged with the chunk that follows it,
  keeping its own `section_path`.

Section-boundary chunking produces clean `section_path` metadata and
embeddings scoped to one topic, because policy documents are authored with
one topic per section. Its cost is that a concept spanning a section break
is invisible to retrieval ([Known simplifications](#known-simplifications)).
Author so that a long `##` section has `###` subsections as natural split
points.

## Indexing

```bash
make build-policy-index-<reasoner_id>   # e.g. make build-policy-index-ato-reasoner
uv run python -m policy_rag_gate.corpus_loader --reasoner-id <reasoner_id> [--corpus-dir <path>] [--wipe]
```

| Option | Meaning |
|---|---|
| `--reasoner-id` | Required. The reasoner that owns the corpus: stored on every chunk (the tenant filter at retrieval) and used to find its corpus directory: the `corpus_dir` of the composed plugin with that `reasoner_id` (plugins filtered by `DECISIONLEDGER_REASONERS`), else `<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus` (DR-33). An id the ledger does not register (plugins, the registration file, the API's registrations), or whose corpus is in neither place, exits 2. |
| `--corpus-dir` | Index this directory instead (for tests, or a directory the gate does not read); the reasoner must still be registered. |
| `--wipe` | Delete every chunk of this reasoner from both stores before loading. |

The loader reads the Postgres DSN from the framework settings (the gate's
schema lives in the framework's database) and the Elasticsearch URL from the
gate's (`POLICY_RAG_GATE_ELASTICSEARCH_URL`). It applies the gate's DDL,
embeds every chunk with `all-MiniLM-L6-v2`, and writes:

- **Postgres** `policy_rag.policy_chunks`: text, metadata, `reasoner_id`,
  and the 384-dimension embedding. Without `--wipe` it replaces each
  document's existing chunks for that reasoner.
- **Elasticsearch** `policy-chunks`: text and metadata, no embedding. The
  loader creates the index from `policy_rag_gate/es/policy-chunks.json`, and
  drops and recreates it when an older index does not map `reasoner_id` as a
  keyword (the tenant filter would silently match nothing); every reasoner's
  corpus must then be reloaded. Without `--wipe` the loader deletes no
  Elasticsearch documents, and every chunk gets a new id on each run, so
  re-indexing an already indexed corpus without `--wipe` leaves its earlier
  BM25 documents in place beside the new ones. Pass `--wipe` when
  re-indexing.

A reasoner's `make prepare-<domain>` target indexes its corpus
(`make prepare-reasoners` indexes every reasoner's). The API does not index
at startup.

## Retrieval filters

At decision time the reasoner's gate context selects what retrieval may
return ([Policy gate](index.md#how-it-decides)):

| Filter | Dense store (pgvector) | Sparse store (Elasticsearch) |
|---|---|---|
| `reasoner_id` (always; tenant isolation) | yes | yes |
| `jurisdictions` | yes | yes |
| `risk_tier` (matches the tier or `null`) | yes | no |

Each retrieved snippet's `jurisdiction` becomes the jurisdiction tag of its
`EvidenceRef`. Enforcement's jurisdiction-conflict tier reads those tags and
checks them against the reasoner's `jurisdiction_conflict_groups`, so a
decision whose evidence spans two conflicting groups is held for review.
The retriever has no version logic: when one document supersedes another,
both are indexed, and only ranking separates them. The gate's retrieval
dimension checks that the current version ranks in the top three for the
queries that target it.

## The ATO reference corpus

Everything from here on belongs to the ATO reasoner
(`reasoners/account_takeover`), whose corpus is
`reasoners/account_takeover/src/ato_reasoner/corpus/`. It has **32
documents** across four jurisdictions and four document types, indexed under
`reasoner_id` `ato-reasoner` (`make build-policy-index-ato-reasoner`, run by
`make prepare-account_takeover`). ATO's jurisdiction and risk-tier values are
the string values of its `Jurisdiction` and `RiskTier` enums
(`ato_reasoner/policy.py`). ATO arranges the corpus by document type:

```
ato_reasoner/corpus/
├── regulations/      # REGULATION: legally binding external sources
├── guidance/         # GUIDANCE: non-binding standards-body documents
├── standards/        # STANDARD: technical and operational standards
└── internal/         # INTERNAL_POLICY: organization-specific policy
```

### Document inventory

#### Regulations (4)

Legally binding instruments, retrieved for the regulatory justification
alongside internal policy citations.

| `policy_id` | Title | Jurisdiction | Key content for ATO decisions |
|---|---|---|---|
| `GLBA-SAFEGUARDS` | Gramm-Leach-Bliley Safeguards Rule | US_FEDERAL | Authentication safeguards, credential protection, incident response obligations |
| `FFIEC-AUTH-GUIDANCE` | FFIEC Authentication in Internet Banking | US_FEDERAL | Risk-based authentication, layered controls, anomalous access patterns |
| `NYDFS-23NYCRR-500` | NYDFS 23 NYCRR Part 500 | US_STATE | MFA requirements, access controls, incident notification for financial services |
| `GDPR-ART32-SECURITY` | GDPR Article 32, Security of Processing | EU_GDPR | Risk-appropriate technical controls, pseudonymization, ongoing evaluation |

#### Guidance (8)

Non-binding best-practice documents from regulatory bodies and standards
organizations, retrieved for the technical grounding of internal controls.

| `policy_id` | Title | Jurisdiction | Key content |
|---|---|---|---|
| `NIST-SP-800-63B` | Digital Identity Guidelines, Authentication | US_FEDERAL | AAL levels, MFA requirements, authenticator assurance, device confidence |
| `NIST-SP-800-53-AC` | Security Controls, Access Control Family | US_FEDERAL | Account management, session controls, least privilege |
| `FFIEC-IT-INFO-SEC` | FFIEC IT Examination Handbook, Information Security | US_FEDERAL | Layered security, monitoring, ATO indicators |
| `OCC-ATO-GUIDANCE` | OCC Guidance on Online Account Takeover | US_FEDERAL | Post-breach response, session suspension, customer notification |
| `CISA-ATO-PREVENTION` | CISA Account Takeover Prevention Guide | US_FEDERAL | Credential stuffing detection, velocity controls, bot mitigation |
| `ENISA-AUTH-GUIDELINES` | ENISA Authentication Guidelines | EU_GDPR | Device binding, geo-based risk, step-up authentication in a GDPR context |
| `NIST-CSF-PR-AC` | NIST CSF, Protect: Access Control | US_FEDERAL | Identity management, authentication policies, remote access controls |
| `FTC-SAFEGUARDS-IMPL` | FTC Safeguards Rule Implementation Guide | US_FEDERAL | Risk assessment, novel entity controls, safeguards for new accounts |

#### Standards (4)

Technical and operational standards, treated as binding where contractually
adopted, retrieved when decisions involve enterprise or high-value accounts.

| `policy_id` | Title | Jurisdiction | Key content |
|---|---|---|---|
| `PCI-DSS-V4-REQ8` | PCI DSS v4.0, Requirement 8: Authentication | INTERNAL | MFA for all access, session management, invalid authentication lockout |
| `ISO-27001-A9` | ISO/IEC 27001 Annex A.9, Access Control | INTERNAL | Access control policy, user authentication, system and application access |
| `SOC2-CC6-ACCESS` | SOC 2 CC6, Logical and Physical Access | INTERNAL | Access provisioning, authentication mechanisms, review and monitoring |
| `FIDO2-WEBAUTHN-CRED` | FIDO2/WebAuthn Credential Management | INTERNAL | Authenticator binding, credential lifecycle, device attestation |

#### Internal policies (16)

Organization-specific policies and procedures, carrying the operational
thresholds the gate reasons against. Two superseded versions exist to
exercise version resolution in retrieval.

| `policy_id` | Version | Supersedes | Risk tier | Jurisdiction | Purpose |
|---|---|---|---|---|---|
| `INT-AUTH-RISK-V1` | 1.0 | — | all | INTERNAL | *Superseded.* Original authentication risk controls; exists to test that V2 ranks above it. |
| `INT-AUTH-RISK-V2` | 2.1 | V1 | all | INTERNAL | Current authentication risk controls. Master threshold document: ALLOW, CHALLENGE, HOLD, and BLOCK criteria. |
| `INT-ATO-DETECT-V1` | 1.0 | — | all | INTERNAL | *Superseded.* Original ATO detection policy. |
| `INT-ATO-DETECT-V2` | 2.0 | V1 | all | INTERNAL | Current ATO detection and response: post-breach indicators, credential compromise handling. |
| `INT-DEVICE-FP-V1` | 1.2 | — | all | INTERNAL | Device fingerprint anomaly response: partial and rotating fingerprint thresholds and actions. |
| `INT-GEO-RISK-V1` | 1.0 | — | all | INTERNAL | Geographic risk controls: the impossible-travel definition and its mandatory BLOCK rule. |
| `INT-VELOCITY-V1` | 1.3 | — | all | INTERNAL | Velocity and rate limiting: per-IP, per-account, and burst-pattern thresholds. |
| `INT-NOVEL-ENTITY-V1` | 1.0 | — | all | INTERNAL | Novel entity risk controls: sparse-history handling, HOLD trigger, CHALLENGE conditions. |
| `INT-CRED-STUFF-V2` | 2.0 | — | all | INTERNAL | Credential stuffing response: multi-account velocity, failure rate, known-bad ASN rules. |
| `INT-MFA-REQ-V2` | 2.0 | — | all | INTERNAL | MFA requirement policy: when step-up authentication is mandatory or optional. |
| `INT-HVA-POLICY-V2` | 2.0 | — | HIGH_VALUE | INTERNAL | High-value account security: stricter thresholds, lower BLOCK floor, mandatory MFA. |
| `INT-ENT-AUTH-V1` | 1.1 | — | ENTERPRISE | INTERNAL | Enterprise account authentication: friction limits, SSO exemptions, velocity overrides. |
| `INT-HOLD-QUEUE-V1` | 1.0 | — | all | INTERNAL | Review queue and HOLD processing: review SLA, escalation paths, resolution criteria. |
| `INT-DATA-MIN-GDPR-V1` | 1.0 | — | all | EU_GDPR | Data minimization and retention (EU compliance): limits on event data stored during decisions. |
| `INT-INCIDENT-ATO-V1` | 1.0 | — | all | INTERNAL | Incident response, account takeover playbook: escalation triggers, containment, notification. |
| `INT-POLICY-GATE-V1` | 1.0 | — | all | INTERNAL | Automated decision policy gate framework: injection resistance, schema violation routing, fallback behavior. |

### Threshold calibration

ATO's internal policy thresholds are deliberately calibrated to the
synthetic scenario generator configurations in
`ato_reasoner/generator/scenarios/`, so the generator and the policy gate
are self-consistent within the reference implementation:

| Signal | Generator config value | Policy document | Policy threshold |
|---|---|---|---|
| Device consistency, ambiguous zone | `partial_match_score: 0.52` | `INT-DEVICE-FP-V1` | score ∈ [0.35, 0.70] → CHALLENGE |
| Device consistency, rotating (new device) | `consistency: rotating` | `INT-DEVICE-FP-V1` | score < 0.35 plus corroborating signals → HOLD or BLOCK |
| Impossible travel | `min_travel_speed_kmh: 1200.0` | `INT-GEO-RISK-V1` | speed > 900 km/h → BLOCK (mandatory) |
| Credential stuffing velocity | `events_per_minute: 120.0` | `INT-VELOCITY-V1` | > 60 requests/min from one source → escalate to BLOCK review |
| Credential stuffing failure rate | `FAILURE: 0.85` | `INT-CRED-STUFF-V2` | failure rate > 70% plus velocity flag → BLOCK |
| High-velocity legitimate | `events_per_minute: 30.0`, `method: SSO` | `INT-ENT-AUTH-V1` | established SSO clients exempt from velocity BLOCK; maximum action CHALLENGE |
| Novel entity | ~1.5 events per user at run time | `INT-NOVEL-ENTITY-V1` | < 10 historical events → `sparse_history=True` → HOLD or CHALLENGE |
| Post-breach ATO success rate | `SUCCESS: 0.80` | `INT-ATO-DETECT-V2` | success rate > 70% plus new device plus geo anomaly → HOLD with review packet |
| Burst detection | `burst_factor: 3.0`, `burst_duration_s: 30` | `INT-VELOCITY-V1` | ≥ 3x baseline velocity for > 15 s → burst flag set |

These thresholds are synthetic. They were chosen to produce deterministic,
evaluable scenario outcomes, not empirically validated fraud thresholds
([Known simplifications](#known-simplifications)).

### Jurisdictions

ATO uses four jurisdictions, covering the regulatory contexts of a
US-headquartered financial services firm with EU data subjects:

| Jurisdiction | Scope | Documents |
|---|---|---|
| `US_FEDERAL` | Federal banking regulation and NIST standards | `GLBA-SAFEGUARDS`, `FFIEC-*`, `OCC-*`, `CISA-*`, `NIST-*`, `FTC-*` |
| `US_STATE` | State-level regulation (NYDFS as the most demanding US state) | `NYDFS-23NYCRR-500` |
| `EU_GDPR` | EU data protection and authentication guidance | `GDPR-ART32-SECURITY`, `ENISA-AUTH-GUIDELINES`, `INT-DATA-MIN-GDPR-V1` |
| `INTERNAL` | Organization-internal policy and standards adopted by contract | Every other `INT-*` document, and `PCI-*`, `ISO-*`, `SOC2-*`, `FIDO2-*` |

ATO's assembler filters retrieval to `INTERNAL`, plus `US_FEDERAL` when the
login is located in the US, so an ATO decision never retrieves GDPR data
minimization requirements as citations. ATO's registration declares two
jurisdiction conflict groups, `{US_FEDERAL, US_STATE}` and `{EU_GDPR}`.
Because its filter never includes `EU_GDPR`, an ATO decision's evidence does
not span both groups, and the jurisdiction-conflict tier holds an ATO
decision only when the model flags a conflict itself (the
`jurisdiction_conflict` control, or an escalation that names one).

### Known simplifications

ATO's corpus is a synthetic demonstration artifact. These are its deliberate
simplifications relative to a production compliance system.

#### Synthetic regulatory text

**What the corpus does:** the regulatory and guidance documents
(`REGULATION`, `GUIDANCE`) are synthetic adaptations: key provisions from
public sources, paraphrased and scoped to identity risk decisions, and
labeled as synthetic reference material.

**What production requires:** verbatim licensed text, or attributed
quotations with stable citation references (CFR section numbers, paragraph
ids). Some standards (ISO 27001, PCI DSS) are not public domain and cannot be
reproduced. Production implementations keep a separately licensed document
store, or index only their own internal policies and cite external authority
by reference.

**Impact:** retrieval and the gate's eval work against synthetic text. The
citation dimension measures the model's reasoning against the corpus as
given; it does not validate that the corpus represents the underlying
regulation. A real implementation needs a separate corpus accuracy review.

#### Section-boundary chunking without overlap

**What the corpus does:** chunks at `##` boundaries (with `###` as a
secondary split for oversized sections), measured in words, with no
overlap between chunks.

**What production requires:** sliding-window chunking with token overlap
(typically 128–256 tokens), so concepts that cross a section boundary are
retrievable; hierarchical chunking (parent-child relationships between
levels) for coarse-to-fine retrieval; and dynamic chunk sizing by semantic
coherence rather than structure.

**Impact:** retrieval misses guidance expressed across a `##` boundary.
Documents are authored with self-contained sections to minimize this, but
it remains a retrieval gap.

#### Thresholds calibrated to the generator

**What the corpus does:** internal policy thresholds match the synthetic
scenario generator ([Threshold calibration](#threshold-calibration)).

**What production requires:** thresholds derived from empirical risk
modeling: historical attack data, false-positive rates from experiments,
calibration against labeled fraud data, and ongoing tuning as attack
patterns evolve.

**Impact:** the corpus and the generator agree by construction. That makes
the eval reproducible, but circular: the system is evaluated against a
ground truth it was partly designed to match. A real evaluation uses
held-out attack data the system has not seen.

#### Static corpus, no update pipeline

**What the corpus does:** it is a fixed set of files; nothing ingests
regulatory updates.

**What production requires:** a pipeline that monitors regulatory sources,
ingests new documents, re-indexes the affected chunks in both stores, bumps
the corpus version the reasoner sends (`corpus_version` in its gate context,
recorded in the gate's snapshot of every decision), and re-runs the gate's
retrieval eval to confirm no regression.

**Impact:** `corpus_version` is recorded on every gated decision but static
across all of them. Version resolution (V2 ranking above V1) is exercised by
the two superseded documents (`INT-AUTH-RISK-V1`, `INT-ATO-DETECT-V1`) but by
no live update.

#### No deep cross-jurisdiction conflict resolution

**What the corpus does:** it spans four jurisdictions, and enforcement holds
a decision whose evidence spans conflicting jurisdiction groups or whose
verdict flags a conflict. Two documents create a surface-level conflict on
purpose: `GDPR-ART32-SECURITY` (data minimization in an EU context) and
`GLBA-SAFEGUARDS` (retention requirements under US federal law).

**What production requires:** a conflict resolution framework that knows
which jurisdiction takes precedence for a data subject's domicile, which
provisions are compatible or mutually exclusive, and how to route decisions
whose obligations cannot be met together. That is a legal engineering
problem, not a retrieval problem.

**Impact:** a decision whose evidence or verdict shows a conflict is held
for human review; the conflict itself is not resolved. ATO's own retrieval
filter keeps `EU_GDPR` documents out of its live decisions.

#### Single language

**What the corpus does:** every document is in English.

**What production requires:** for `EU_GDPR`, guidance is authoritative in
several official EU languages, which needs language-aware retrieval or
machine translation with quality controls.

**Impact:** none within this implementation; the `EU_GDPR` documents are
English-language synthetic adaptations.

#### No freshness decay

**What the corpus does:** every document is treated as equally current
regardless of `effective_date`.

**What production requires:** recency weighting during retrieval, so a newer
version of a piece of guidance ranks above an older one for the same query.
That is separate from supersession (explicit V1 → V2 chains): freshness decay
handles documents that are not formally superseded but have been overtaken
by newer guidance.

**Impact:** not exercised; the documents' `effective_date` values fall in a
narrow window.

### Authoring guidelines

These conventions keep retrieval and citation quality high:

1. **One claim per paragraph.** Each paragraph should be citable as a unit,
   so the model does not have to cherry-pick within a citation.
2. **Normative language in internal policies.** Use SHALL, MUST, and SHOULD
   (RFC 2119) to distinguish mandatory from recommended controls; it tells
   the model how strong a control is.
3. **Explicit threshold values.** Put a quantitative threshold (a speed, a
   rate, a score) in the section most likely to be retrieved for the
   corresponding scenario, not in an appendix.
4. **Cross-references by `policy_id`.** When an internal policy references an
   external requirement, name its `policy_id` ("as required by
   `FFIEC-AUTH-GUIDANCE` §4.1") so the model can trace the citation chain.
5. **Mark superseded documents.** `INT-AUTH-RISK-V1` and `INT-ATO-DETECT-V1`
   open with a prominent notice ("THIS DOCUMENT IS SUPERSEDED. Do not cite.")
   naming the replacing `policy_id`. Retrieval still indexes them; the
   retrieval eval checks that the current version outranks them, and the
   notice keeps the model from citing a superseded document that is
   retrieved anyway.
