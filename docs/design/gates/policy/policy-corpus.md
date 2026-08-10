# Policy Corpus

## Overview

The policy corpus is the raw material that feeds the policy RAG retrieval layer
(C6). During a live decision, the retriever queries the corpus to surface the most
relevant policy chunks for a given event context. Those chunks are passed to the
LLM policy gate (C7), which reasons against them to produce a structured
`PolicyGateOutput` with citations. The quality of the corpus determines
the quality of the gate's reasoning: sparse or vague documents produce vague
citations; precise, well-scoped documents produce grounded, citable decisions.

The corpus consists of **32 documents** across four regulatory jurisdictions and
four document types. Documents are stored as Markdown files with YAML frontmatter
in `corpus/` at the project root. The frontmatter maps directly to the
`PolicyDocument` schema in `core/policy/corpus.py`. The retrieval layer at
`app/retrieval/` loads, chunks, embeds, and indexes the corpus at startup.

---

## The Two-Layer Citation Model

Every policy gate decision produces two kinds of citations, and the corpus is
designed to support both:

**Layer 1 — The WHAT (internal policy documents)**
Specific, actionable rules with concrete thresholds. "Impossible travel events —
where consecutive login speed exceeds 900 km/h — SHALL be blocked without
exception." These are what the enforcement layer executes against. The LLM cites
these to explain what action was taken.

**Layer 2 — The WHY (regulatory and guidance documents)**
The regulatory justification for those rules. "FFIEC guidance requires institutions
to assess anomalous geographic access patterns as part of layered security
controls." These are why the internal policy rules exist. The LLM cites these to
ground the decision in external authority.

This structure reflects real compliance reasoning: internal policies operationalize
regulatory requirements. The gate needs both layers to produce grounded, complete
rationale — citing only internal policy produces circular reasoning; citing only
regulation produces detached reasoning with no operational specificity.

The evaluation harness tests citation quality against both layers (eval dimension
04 — Citation Accuracy). A citation that retrieves the right section from the
wrong layer is a partial credit failure; a citation that retrieves neither is a
blocking failure.

---

## Format and Storage

### File format

Each document is a Markdown file with YAML frontmatter:

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

This policy establishes geographic risk controls for login event processing...

## 2. Travel Speed Classification

...
```

The frontmatter fields map directly to `PolicyDocument` in
`core/policy/corpus.py`. The `jurisdiction` and `risk_tier` fields use the string
values of the `Jurisdiction` and `RiskTier` enums from
`core/policy/corpus.py`.

### Storage location

Corpus documents live in `corpus/` at the project root, not inside `app/`.
This is an intentional boundary: the corpus is versioned source data, not
application code. The retrieval layer at `app/retrieval/` imports and indexes it,
but does not own it. This separation makes corpus updates (adding a document,
correcting a threshold) independent of application code changes.

```
corpus/
├── regulations/      # REGULATION type — legally binding external sources
├── guidance/         # GUIDANCE type — non-binding standards body documents
├── standards/        # STANDARD type — technical/operational standards
└── internal/         # INTERNAL_POLICY type — organisation-specific policy
```

---

## Chunking Strategy

The retriever chunks documents at `##` section heading boundaries. Each chunk
becomes one `PolicySnippet` with:

- `section_path` set to the heading text (e.g., `"3. Impossible Travel Controls"`)
- `text` set to the full text under that heading, up to the next `##`
- `policy_id`, `title`, `version`, `jurisdiction` inherited from frontmatter

**Rationale for section-boundary chunking:** Policy documents are authored with
section structure that corresponds to topical scope. A section on impossible
travel controls contains exactly the concepts relevant to an impossible-travel
query, and nothing irrelevant. This produces cleaner `section_path` metadata and
more precise dense embeddings than sliding-window chunking.

**Known limitation:** Concepts that span section breaks are invisible to the
retriever. See [Known Simplifications](#known-simplifications).

Target chunk size: 115–300 words (measured by word count; approximately 150–400
tokens at the ~1.3 tokens-per-word ratio for English prose). The implementation
uses word count directly — no tokenizer dependency.

**Oversized sections:** When a `##` section exceeds 300 words, it is split at
`###` subsection boundaries rather than at arbitrary paragraph breaks. Each `###`
subsection becomes its own chunk with a compound `section_path`:

```
"4. Action Decision Matrix > 4.2 CHALLENGE Criteria"
```

This avoids duplicate `section_path` values on the same document, which would
produce ambiguous citations and potential index collisions. Corpus documents are
authored so that `##` sections containing substantial content use `###`
subsections as natural split points.

Sections shorter than 30 words are merged with the following sibling section
under the same `##` heading, inheriting that heading's `section_path`.

---

## Document Inventory

### Regulations (4)

Legally binding instruments. Highest retrieval authority. Retrieved to provide
regulatory justification alongside internal policy citations.

| `policy_id` | Title | Jurisdiction | Key content for ATO decisions |
|---|---|---|---|
| `GLBA-SAFEGUARDS` | Gramm-Leach-Bliley Safeguards Rule | US_FEDERAL | Authentication safeguards, credential protection, incident response obligations |
| `FFIEC-AUTH-GUIDANCE` | FFIEC Authentication in Internet Banking | US_FEDERAL | Risk-based authentication, layered controls, anomalous access patterns |
| `NYDFS-23NYCRR-500` | NYDFS 23 NYCRR Part 500 | US_STATE | MFA requirements, access controls, incident notification for financial services |
| `GDPR-ART32-SECURITY` | GDPR Article 32 — Security of Processing | EU_GDPR | Risk-appropriate technical controls, pseudonymisation, ongoing evaluation |

### Guidance (8)

Non-binding best-practice documents from regulatory bodies and standards
organisations. Retrieved to provide technical grounding for internal controls.

| `policy_id` | Title | Jurisdiction | Key content |
|---|---|---|---|
| `NIST-SP-800-63B` | Digital Identity Guidelines — Authentication | US_FEDERAL | AAL levels, MFA requirements, authenticator assurance, device confidence |
| `NIST-SP-800-53-AC` | Security Controls — Access Control Family | US_FEDERAL | Account management, session controls, least privilege |
| `FFIEC-IT-INFO-SEC` | FFIEC IT Examination Handbook — Information Security | US_FEDERAL | Layered security, monitoring, ATO indicators |
| `OCC-ATO-GUIDANCE` | OCC Guidance on Online Account Takeover | US_FEDERAL | Post-breach response, session suspension, customer notification |
| `CISA-ATO-PREVENTION` | CISA Account Takeover Prevention Guide | US_FEDERAL | Credential stuffing detection, velocity controls, bot mitigation |
| `ENISA-AUTH-GUIDELINES` | ENISA Authentication Guidelines | EU_GDPR | Device binding, geo-based risk, step-up authentication for GDPR context |
| `NIST-CSF-PR-AC` | NIST CSF — Protect: Access Control | US_FEDERAL | Identity management, authentication policies, remote access controls |
| `FTC-SAFEGUARDS-IMPL` | FTC Safeguards Rule Implementation Guide | US_FEDERAL | Risk assessment, novel entity controls, safeguards for new accounts |

### Standards (4)

Technical and operational standards. Treated as binding where contractually
adopted. Retrieved when decisions involve enterprise or high-value accounts.

| `policy_id` | Title | Jurisdiction | Key content |
|---|---|---|---|
| `PCI-DSS-V4-REQ8` | PCI DSS v4.0 — Requirement 8: Authentication | INTERNAL | MFA for all access, session management, invalid authentication lockout |
| `ISO-27001-A9` | ISO/IEC 27001 Annex A.9 — Access Control | INTERNAL | Access control policy, user authentication, system and application access |
| `SOC2-CC6-ACCESS` | SOC 2 CC6 — Logical and Physical Access | INTERNAL | Access provisioning, authentication mechanisms, review and monitoring |
| `FIDO2-WEBAUTHN-CRED` | FIDO2/WebAuthn Credential Management | INTERNAL | Authenticator binding, credential lifecycle, device attestation |

### Internal Policies (16)

Organisation-specific policies and procedures. These contain the operational
thresholds the enforcement layer executes against. Two superseded versions exist
to exercise version resolution logic in the retriever.

| `policy_id` | Version | Supersedes | Risk Tier | Jurisdiction | Purpose |
|---|---|---|---|---|---|
| `INT-AUTH-RISK-V1` | 1.0 | — | all | INTERNAL | *Superseded.* Original auth risk controls. Exists to test version resolution — retriever must prefer V2. |
| `INT-AUTH-RISK-V2` | 2.1 | V1 | all | INTERNAL | Current authentication risk controls. Master threshold document. Covers ALLOW/CHALLENGE/HOLD/BLOCK decision criteria. |
| `INT-ATO-DETECT-V1` | 1.0 | — | all | INTERNAL | *Superseded.* Original ATO detection policy. |
| `INT-ATO-DETECT-V2` | 2.0 | V1 | all | INTERNAL | Current ATO detection and response. Post-breach ATO indicators, credential compromise handling. |
| `INT-DEVICE-FP-V1` | 1.2 | — | all | INTERNAL | Device fingerprint anomaly response. Partial/rotating fingerprint thresholds and actions. |
| `INT-GEO-RISK-V1` | 1.0 | — | all | INTERNAL | Geographic risk controls. Impossible travel definition and mandatory BLOCK rule. |
| `INT-VELOCITY-V1` | 1.3 | — | all | INTERNAL | Velocity and rate limiting. Per-IP, per-account, and burst-pattern thresholds. |
| `INT-NOVEL-ENTITY-V1` | 1.0 | — | all | INTERNAL | Novel entity risk controls. Sparse-history routing, HOLD trigger, CHALLENGE conditions. |
| `INT-CRED-STUFF-V2` | 2.0 | — | all | INTERNAL | Credential stuffing response. Multi-account velocity, failure rate, known-bad ASN rules. |
| `INT-MFA-REQ-V2` | 2.0 | — | all | INTERNAL | MFA requirement policy. When step-up authentication is mandatory vs. optional. |
| `INT-HVA-POLICY-V2` | 2.0 | — | HIGH_VALUE | INTERNAL | High-value account security. Stricter thresholds, lower BLOCK floor, mandatory MFA. |
| `INT-ENT-AUTH-V1` | 1.1 | — | ENTERPRISE | INTERNAL | Enterprise account authentication. SLA-aware friction limits, SSO exemptions, velocity overrides. |
| `INT-HOLD-QUEUE-V1` | 1.0 | — | all | INTERNAL | Review queue and HOLD processing. SLA for human review, escalation paths, resolution criteria. |
| `INT-DATA-MIN-GDPR-V1` | 1.0 | — | all | EU_GDPR | Data minimisation and retention (EU compliance). Limits on event data stored during decisions. |
| `INT-INCIDENT-ATO-V1` | 1.0 | — | all | INTERNAL | Incident response — account takeover playbook. Escalation triggers, containment, notification. |
| `INT-POLICY-GATE-V1` | 1.0 | — | all | INTERNAL | Automated decision policy gate framework. Injection resistance, schema violation routing, fallback behaviour. |

---

## Threshold Calibration

Internal policy thresholds are deliberately calibrated to match the synthetic
scenario generator configurations in `generator/scenarios/`. This ensures the
generator and policy gate are self-consistent within the reference implementation.

The following table maps generator config values to the policy thresholds written
into the corresponding internal policy documents:

| Signal | Generator config value | Policy document | Policy threshold |
|---|---|---|---|
| Device consistency — ambiguous zone | `partial_match_score: 0.52` | `INT-DEVICE-FP-V1` | score ∈ [0.35, 0.70] → CHALLENGE |
| Device consistency — rotating (new device) | `consistency: rotating` | `INT-DEVICE-FP-V1` | score < 0.35 + corroborating signals → HOLD/BLOCK |
| Impossible travel | `min_travel_speed_kmh: 1200.0` | `INT-GEO-RISK-V1` | speed > 900 km/h → BLOCK (mandatory) |
| Credential stuffing velocity | `events_per_minute: 120.0` | `INT-VELOCITY-V1` | > 60 req/min from same source → escalate to BLOCK review |
| Credential stuffing failure rate | `FAILURE: 0.85` | `INT-CRED-STUFF-V2` | failure rate > 70% + velocity flag → BLOCK |
| High-velocity legitimate | `events_per_minute: 30.0`, `method: SSO` | `INT-ENT-AUTH-V1` | established SSO clients exempt from velocity-BLOCK; max action = CHALLENGE |
| Novel entity threshold | ~1.5 events/user at run time | `INT-NOVEL-ENTITY-V1` | < 10 historical events → sparse_history=True → HOLD or CHALLENGE |
| Post-breach ATO success rate | `SUCCESS: 0.80` | `INT-ATO-DETECT-V2` | success rate > 70% + new device + geo anomaly → HOLD with review packet |
| Burst detection | `burst_factor: 3.0`, `burst_duration_s: 30` | `INT-VELOCITY-V1` | ≥3x baseline velocity for >15s → burst flag set |

**Important:** These thresholds are synthetic. They were chosen to produce
deterministic, evaluable scenario outcomes, not to reflect empirically validated
fraud thresholds from real deployment data. See
[Known Simplifications](#known-simplifications).

---

## Jurisdiction Design

Four jurisdictions cover the three most common regulatory contexts for identity
risk decisions in a US-headquartered financial services firm with EU data subjects:

| Jurisdiction | Scope | Documents |
|---|---|---|
| `US_FEDERAL` | Federal banking regulation and NIST standards | `GLBA-SAFEGUARDS`, `FFIEC-*`, `OCC-*`, `CISA-*`, `NIST-*`, `FTC-*` |
| `US_STATE` | State-level regulation (NYDFS as the most demanding US state) | `NYDFS-23NYCRR-500` |
| `EU_GDPR` | EU data protection and authentication guidance | `GDPR-ART32-SECURITY`, `ENISA-AUTH-GUIDELINES`, `INT-DATA-MIN-GDPR-V1` |
| `INTERNAL` | Organisation-internal policy, standards adopted by contract | All `INT-*` and `PCI-*`, `ISO-*`, `SOC2-*`, `FIDO2-*` |

The retrieval metadata filter uses these values to prevent cross-jurisdiction
contamination. A US-only decision context should not retrieve GDPR data
minimisation requirements as primary citations. The `PolicySnippet` carries the
`jurisdiction` field so the enforcement layer can flag cross-jurisdiction
conflicts in the `override_log`.

---

## Known Simplifications

This corpus is a synthetic demonstration artifact. The following table documents
deliberate simplifications relative to what a production compliance system would
require.

### Synthetic regulatory text

**What this corpus does:** Regulatory and guidance documents (`REGULATION`,
`GUIDANCE` types) are written as synthetic adaptations. Key provisions from
publicly available sources are paraphrased and scoped to identity risk
decisions. Documents are clearly labeled as synthetic reference material in
their frontmatter.

**What production requires:** Verbatim licensed text, or clearly attributed
quotations with stable citation references (CFR section numbers, paragraph IDs).
Some standards (ISO 27001, PCI DSS) are not public-domain and cannot be
reproduced. Production implementations would typically maintain a separately
licensed document store, or operate only against their own internal policy
documents while citing external authority by reference rather than by chunk.

**Impact:** The RAG retriever and eval harness work correctly against synthetic 
text. Citation quality eval (dimension 04) measures LLM reasoning quality
against the corpus as given — it does not validate that the corpus itself
accurately represents the underlying regulation. A real implementation would
need a separate corpus accuracy review.

### Section-boundary chunking without overlap

**What this corpus does:** Chunk at `##` boundaries (with `###` as a secondary
split for oversized sections). Word count as size metric. No inter-chunk overlap.

**What production requires:** Sliding-window chunking with token overlap
(typically 128–256 tokens) ensures that concepts expressed across a section
boundary are visible to the retriever. Hierarchical chunking (parent-child
relationships between `#` and `##` sections) provides coarse-to-fine retrieval.
Dynamic chunk sizing based on semantic coherence rather than structural
boundaries further improves precision.

**Impact:** The retriever will miss policy guidance expressed in sentences that
straddle a `##` boundary. Documents are authored to minimise this — each section
is written as a self-contained unit — but it remains a retrieval gap.

### Threshold values are calibrated to the generator

**What this corpus does:** Internal policy thresholds are set to be consistent
with the synthetic scenario generator configurations (see [Threshold
Calibration](#threshold-calibration)).

**What production requires:** Thresholds derived from empirical risk modelling:
historical ATO attack data, false positive rates from A/B testing, calibration
against labelled fraud datasets, and ongoing threshold tuning as attack patterns
evolve.

**Impact:** The corpus and generator are self-consistent by construction. This
makes the eval harness deterministic and reproducible, but creates a circularity:
the system is evaluated against a ground truth it was partly designed to match.
A real evaluation would use held-out attack data the system has not seen.

### Static corpus — no update pipeline

**What this corpus does:** The corpus is a fixed set of files in `corpus/`. No
ingestion workflow exists for regulatory updates.

**What production requires:** A corpus update pipeline that monitors regulatory
sources, ingests new documents, updates affected chunks in pgvector and
Elasticsearch, version-bumps the `policy_corpus_version` recorded in every
Decision Bundle, and triggers a re-run of retrieval eval (dimension 01) to confirm
no regression.

**Impact:** The `policy_corpus_version` field in `DecisionBundle` is populated but
static across all decisions in this implementation. The version resolution logic
(preferring V2 over V1 for superseded documents) is exercised by the two
superseded documents (`INT-AUTH-RISK-V1`, `INT-ATO-DETECT-V1`) but not by any
live update scenario.

### No deep cross-jurisdiction conflict resolution

**What this corpus does:** The corpus includes four jurisdictions and the enforcement layer
flags jurisdiction conflicts in the `override_log`. Two documents intentionally
create a surface-level conflict: `GDPR-ART32-SECURITY` (data minimisation in EU
context) vs. `GLBA-SAFEGUARDS` (retention requirements under US federal law).

**What production requires:** A structured conflict resolution framework that
knows which jurisdiction takes precedence for a given data subject's domicile,
which provisions are compatible vs. mutually exclusive, and how to route decisions
where the obligations cannot be simultaneously satisfied. This is a significant
legal engineering problem, not just a retrieval problem.

**Impact:** The policy gate will encounter the conflict in multi-jurisdiction
queries and is expected to flag `escalate_to_human=True` with an
`escalation_reason` describing the conflict. The eval harness asserts this
escalation occurs. Actual conflict resolution is deferred to human review.

### Single language

**What this corpus does:** All corpus documents are in English.

**What production requires:** For EU_GDPR jurisdiction, regulatory guidance is
authoritative in multiple official EU languages. Language-aware retrieval (or
machine translation with quality controls) is required for multi-language corpora.

**Impact:** None within this implementation. The `EU_GDPR` documents are
English-language synthetic adaptations.

### No freshness decay

**What we do:** All documents are treated as equally current regardless of
`effective_date`.

**What production requires:** Recency weighting during retrieval — a 2024 FFIEC
guidance update should rank above a 2019 version of the same guidance for the
same query. Separate from the supersession mechanism (which handles explicit V1 →
V2 chains), freshness decay handles documents that are not formally superseded
but have been contextually overtaken by newer guidance.

**Impact:** Not exercised in this corpus. All documents are authored with
`effective_date` values within a 2-year window, minimising the practical impact.

---

## Authoring Guidelines

The following conventions apply to all corpus documents to ensure retrieval and
citation quality:

1. **One claim per paragraph.** Each paragraph should be citable as a unit. Avoid
   multi-claim paragraphs that force the LLM to cherry-pick within a citation.

2. **Normative language in internal policies.** Use SHALL, MUST, and SHOULD
   (RFC 2119) to distinguish mandatory from recommended controls. This gives the
   policy gate clear signal about enforcement strength.

3. **Explicit threshold values.** Quantitative thresholds (speeds, rates,
   scores) must appear in the section most likely to be retrieved for the
   corresponding scenario query. Do not bury thresholds in appendices.

4. **Cross-references by `policy_id`.** When an internal policy references an
   external requirement, use the `policy_id` (e.g., "as required by
   `FFIEC-AUTH-GUIDANCE` §4.1") so the LLM can trace the citation chain.

5. **Superseded document marking.** `INT-AUTH-RISK-V1` and `INT-ATO-DETECT-V1`
   must contain a prominent notice in their first section: "This document is
   superseded by [policy_id]. Do not cite." This tests that the retriever's
   version filter prevents stale documents from surfacing, and that the LLM does
   not cite them even if they are erroneously retrieved.
