# Decision Records

Decision records document significant choices made during the design and
implementation of DecisionLedger — what was chosen, why, what was rejected,
and what the tradeoffs are. Future contributors (and anyone evaluating the
portfolio) should be able to reconstruct the reasoning without access to
working memory or out-of-band conversations.

Format follows [Michael Nygard's Architecture Decision Records][adr].

[adr]: https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions

---

## Framework

### When to write a decision record

Write a DR when a choice:

- Is difficult to reverse without meaningful rework
- Has non-obvious tradeoffs that a future contributor would need to understand
- Explicitly rejects one or more reasonable alternatives
- Is likely to prompt the question "why didn't you just use X?"

Routine implementation choices (which library function to use, how to name a
variable, minor structural preferences) do not need DRs. The bar is: would a
capable engineer reading the code a year from now be genuinely uncertain why
this approach was taken?

### How to add a record

Increment from the current head and append to this file. Do not edit the body
of an accepted record — if a decision changes, open a new record that
supersedes it and update the original record's status to `Superseded by DR-N`.

**Current head: DR-21.** The next record is DR-22.

### Status values

| Status | Meaning |
|--------|---------|
| `Proposed` | Under consideration — not yet binding |
| `Accepted` | In force — implementation follows this decision |
| `Superseded by DR-N` | Replaced by a later decision; see DR-N for current guidance |
| `Deprecated` | No longer applicable; retained for historical context |

### Template

```markdown
## DR-N: Short descriptive title

**Status:** Proposed
**Category:** Infrastructure & Orchestration

### Context

What problem, constraint, or requirement prompted this decision?

### Decision

What was chosen and how it works.

### Consequences

What does this enable, what does it foreclose, what are the known tradeoffs,
and what alternatives were explicitly rejected and why?
```

### Categories

Categories are tracked per-record so that navigation grouping and potential
file split-out after MVP require no retroactive changes to record content.

| Category | Scope |
|----------|-------|
| **Infrastructure & Orchestration** | Queue technology, container orchestration, deployment strategy, local dev setup |
| **Data Storage & Retrieval** | Databases, vector stores, retrieval architecture, indexing strategy |
| **Pipeline & System Design** | Latency budgets, routing logic, module and dependency boundaries |
| **Data Strategy** | Training data, labeling functions, synthetic event generation |
| **Tooling & DX** | Documentation tooling, diagramming, developer experience choices |

> Navigation grouping and potential split into per-category files is deferred
> until after MVP ships. The category field on each record below is the only
> change needed when that restructure happens.

---

## DR-1: Redis Streams over Kafka for the reference implementation

**Status:** Accepted
**Category:** Infrastructure & Orchestration

### Context

The event ingestion layer needs consumer groups, acknowledgment semantics,
message replay, and bounded-lateness handling. The reference implementation
must be runnable with a single `docker compose up` command by anyone who clones
the repo.

### Decision

Use Redis Streams with consumer groups as the event queue for the reference
implementation. Document Kafka/Kinesis as the production migration path.

### Consequences

- A reader can run the full system locally without configuring a Kafka cluster
  (ZooKeeper/KRaft, 3+ broker containers, topic configuration). The barrier to
  evaluating the architecture drops to zero.
- Redis Streams provides the semantics needed to demonstrate the pattern:
  consumer groups, acknowledgment, idempotent processing, and message replay.
- At production scale, Redis Streams lacks Kafka's partitioning, throughput
  ceiling, multi-DC replication, and tooling ecosystem. The migration path is
  documented: swap the queue adapter, retain the same consumer group contract.
  The `core/` contracts are queue-agnostic by design.
- Alternatives rejected: Kafka/Redpanda (operational overhead for a reference
  implementation), AWS Kinesis (external dependency, not self-contained),
  RabbitMQ (weaker replay semantics), in-process queue (no persistence, no
  replay).

---

## DR-2: pgvector over managed vector database

**Status:** Accepted
**Category:** Data Storage & Retrieval

### Context

The policy RAG retriever needs dense vector storage and similarity search for
semantic retrieval over the policy corpus. The reference implementation must be
fully self-contained — no external accounts, no API keys beyond the LLM
provider.

### Decision

Use the pgvector extension on PostgreSQL (HNSW index) for vector storage and
similarity search.

### Consequences

- The reference implementation is fully self-contained. Anyone can fork and run
  it without creating a Pinecone/Weaviate/Qdrant account. Reproducibility is
  guaranteed.
- Using pgvector alongside Elasticsearch demonstrates understanding of vector
  storage as an infrastructure concern with real tradeoffs, rather than
  treating it as a managed-service abstraction.
- pgvector's HNSW index performance degrades past ~1M vectors. At that scale, a
  purpose-built vector database (Weaviate, Qdrant) is the right choice. This
  threshold is documented here as a known scaling boundary.
- Alternatives rejected: Pinecone (external dependency, account required),
  Weaviate/Qdrant (operational overhead for <1M vectors), Chroma (limited
  production maturity at the time of decision), Milvus (heavier operational
  footprint than pgvector for this corpus size).

---

## DR-3: Hybrid retrieval (dense + sparse) over dense-only

**Status:** Accepted
**Category:** Data Storage & Retrieval

### Context

The policy corpus contains both semantic concepts (risk posture, authentication
strength, identity assurance) and exact regulatory identifiers (NIST AAL2,
PCI-DSS Requirement 8.3.6, specific section numbers). A retrieval strategy must
handle both effectively.

### Decision

Use Reciprocal Rank Fusion (RRF) of dense retrieval (pgvector) and sparse
retrieval (Elasticsearch BM25), with cross-encoder reranking for final top-k
selection.

### Consequences

- Dense retrieval captures semantic similarity; sparse retrieval captures exact
  term matching. Hybrid fusion handles queries that require either or both — a
  query about "PCI-DSS 8.3.6" surfaces the exact section (BM25), while a query
  about "what constitutes strong authentication" surfaces semantically relevant
  guidance (dense).
- The cross-encoder reranker adds a second-stage quality pass shown to
  meaningfully improve precision on domain-specific corpora.
- Hybrid retrieval has higher latency than either approach alone: two index
  queries + RRF fusion + reranking vs. a single vector query. A configurable
  latency-budget bypass skips the reranker when inference time exceeds the
  configured threshold, falling back to RRF-only results. Which path was taken
  is logged in the Decision Bundle.
- Alternatives rejected: dense-only (misses exact regulatory identifiers),
  sparse-only (misses semantic paraphrases), ColBERT late interaction (higher
  implementation complexity without proportionate quality gain for this corpus
  size).

---

## DR-4: Hierarchical chunking over fixed-size for policy documents

**Status:** Accepted
**Category:** Data Storage & Retrieval

### Context

Policy documents have deliberate hierarchical structure — numbered sections,
subsections, normative requirements, and informative notes. The chunking
strategy affects retrieval quality and the policy gate's ability to cite
relevant evidence.

### Decision

Use hierarchical chunking (document → section → subsection → paragraph) with
section headers preserved as metadata on every chunk. Implement the
parent-document retriever pattern so that when a subsection is retrieved, the
full parent section is available for context injection.

### Consequences

- Section structure is preserved. NIST 800-63B's "Section 5.2.3 — Authenticator
  Assurance Level 2" stays intact as a coherent unit rather than being split at
  a token boundary mid-requirement.
- Section headers as chunk metadata enable the retriever to surface the correct
  section even when a subsection is the most relevant match.
- The parent-document retriever pattern provides context injection: the LLM sees
  the subsection that matched plus the full parent section for context, reducing
  hallucination from context-free snippets.
- Hierarchical chunking requires document structure understanding (section
  detection) that fixed-size chunking doesn't. This investment pays off for
  well-structured regulatory documents. For unstructured text, fixed-size
  chunking is adequate — the chunking strategy is configurable per document
  type.
- Alternatives rejected: fixed-size 512-token chunks (splits mid-section,
  generates retrieval noise), sentence-level (too granular for policy
  documents), whole-document (exceeds context window, low precision).

---

## DR-5: Latency budget allocation across pipeline components

**Status:** Proposed — to be updated with measured P50/P95 numbers
**Category:** Pipeline & System Design

### Context

The ATO Reasoner operates in a real-time decisioning context. Each pipeline component
(ingestion, feature computation, scoring, retrieval, reranking, LLM inference,
enforcement) consumes part of the total latency budget. The LLM policy gate is
the dominant contributor.

### Decision

Target budget allocation: ingestion <5ms, feature computation <15ms, scoring
<10ms, retrieval + reranking <150ms (with reranker bypass fallback per DR-3),
LLM inference <2000ms, enforcement <5ms. Total target: <2200ms for the full
LLM path; <30ms for the fast-scorer-only path.

### Consequences

- LLM inference dominates the budget. Strategies considered: smaller model
  (faster but lower quality), reduced context window (faster but lower
  faithfulness), async policy gate with synchronous enforcement (complex,
  deferred), streaming output (UX improvement, same total latency).
- The cross-encoder reranker is the second-largest contributor. The
  latency-budget bypass (DR-3) ensures the pipeline degrades gracefully when
  reranking exceeds its allocation.
- The fast-scorer-only path (<30ms) handles the majority of volume —
  high-confidence events that don't need LLM elaboration. This is the latency
  argument for the hybrid architecture.
- Per-component latency is recorded in every Decision Bundle, enabling ongoing
  budget analysis from production telemetry.

---

## DR-6: Rule-based human review routing over confidence-threshold-only

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

Some decisions require human review: low-confidence outputs, novel entities,
adversarial patterns, schema validation failures, and cross-jurisdiction policy
conflicts. The routing mechanism must be deterministic and auditable — not
dependent on the LLM's own self-assessment.

### Decision

Use rule-based routing triggers for human review escalation. Five trigger types:
(1) confidence below threshold AND action is BLOCK or HOLD, (2) entity has
fewer than N historical events (novel entity), (3) adversarial probe flag from
preprocessing, (4) schema validation failure on LLM output, (5) conflicting
policy guidance across jurisdictions in retrieved evidence.

### Consequences

- Human review routing is deterministic and auditable. Each trigger has a
  defined priority and produces a pre-populated review packet with the relevant
  Decision Bundle data.
- Avoids a circular dependency: using the LLM's own confidence score as the
  sole routing signal means the LLM decides whether its output is reliable
  enough to trust. The rule-based triggers are independent of the LLM's
  self-assessment.
- Schema validation failures always route to HOLD — never to silent
  enforcement. An unvalidated LLM output cannot reach the enforcement layer.
- The trigger set is extensible: adding new routing rules is a configuration
  change in the enforcement layer, not an LLM prompt change.
- Alternatives rejected: confidence-threshold-only (circular dependency),
  risk-score-threshold-only (ignores LLM output quality), always-route-BLOCK
  (too aggressive, high human review volume), ML-based routing classifier (adds
  model management complexity for marginal improvement over transparent rules).

---

## DR-7: Docker Compose over Kubernetes for the reference implementation

**Status:** Accepted
**Category:** Infrastructure & Orchestration

### Context

The reference implementation needs a local orchestration strategy that allows
the full stack (Redis, PostgreSQL+pgvector, Elasticsearch, API services) to run
with minimal friction for evaluation purposes. The author's professional
background includes Kubernetes at scale.

### Decision

Use Docker Compose V2 (`docker compose up`) for local development and the
reference implementation. Document the production Kubernetes architecture
explicitly rather than implementing it.

### Consequences

- A reader evaluates the architecture with a single command. No Minikube, no
  k3s, no cluster management overhead before they reach the interesting parts.
- Docker Compose V2 with the full stack runs cleanly on an M1 Max 64GB without
  resource constraints.
- The production Kubernetes architecture is documented: Deployments for the API
  and feature service, StatefulSet for Redis with persistent volumes, HPA on
  the policy gate service for variable LLM inference latency, and graceful
  shutdown handling for the feature service's in-flight sliding window state
  during horizontal scaling.
- Knowing when Kubernetes is and isn't the right tool for the context at hand
  is itself the more sophisticated signal.
- Alternatives rejected: Kubernetes with Minikube/k3s locally (operational
  friction before any application code runs), Docker Desktop Kubernetes
  (resource-heavy, stability issues), cloud dev environment (external
  dependency, cost).

---

## DR-8: `core/` module with zero infrastructure dependencies

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

The monorepo contains pure business logic (schemas, contracts, enforcement
rules, evaluation metric interfaces) alongside infrastructure-dependent runtime
code (Redis, PostgreSQL, Elasticsearch, OpenAI API). These concerns must be
separable for testability, readability, and future packaging.

### Decision

Enforce a strict internal dependency boundary: `core/` contains all Pydantic
schemas, contracts, and interfaces with zero infrastructure dependencies (no
Redis, no database drivers, no LLM SDK, no FastAPI). `app/`, `eval/`, and
`generator/` import from `core/`. `core/` imports from nothing internal.

### Consequences

- `core/` logic is unit-testable without Docker running. Decision schemas,
  enforcement rules, and evaluation metric contracts are tested with pure Python
  — fast, deterministic, and CI-friendly.
- A reviewer can read `core/` and understand the entire decision model without
  understanding any infrastructure. The business logic is separable from the
  infrastructure.
- If the project outgrows the reference implementation, adding a `pyproject.toml`
  to `core/` and publishing to PyPI is trivial — the packaging boundary is
  already enforced. A formal published package is deliberately deferred; the
  architectural discipline is the point.
- The monorepo structure (over polyrepo) was chosen because the scenario
  generator, eval harness, and pipeline all evolve together during the reference
  implementation phase. Cross-component changes are a single PR, not a
  coordinated multi-repo release.
- Alternatives rejected: flat module structure (no boundary enforcement),
  separate published package (premature packaging), polyrepo (coordination
  overhead during rapid iteration).

---

## DR-9: Mermaid for README diagrams, PlantUML C4 for documentation site

**Status:** Superseded by DR-15
**Category:** Tooling & DX

### Context

Architecture diagrams must be version-controlled, diffable in PRs, and
renderable without external tooling for the most common viewing contexts
(GitHub README, MkDocs documentation site).

### Decision

Use Mermaid (fenced code blocks in Markdown) for the README pipeline flowchart
and decision path. Use PlantUML with the C4-PlantUML library for
container-level architecture diagrams in the MkDocs docs site. Both committed
as text files — no binary exports.

### Consequences

- Mermaid renders natively on GitHub with zero setup — a fenced ` ```mermaid `
  block appears as an inline diagram for any visitor. This matters for a public
  portfolio repo where the first impression is immediate and frictionless.
- PlantUML with C4-PlantUML provides richer C4 model support than Mermaid's C4
  diagram type: proper context, container, and component diagram hierarchy with
  correct C4 notation. The MkDocs `plantuml-markdown` plugin renders
  server-side, so docs site visitors see diagrams without local tooling.
- Architecture diagrams are diffable in PRs. When the system changes, the
  diagram text changes in the same PR — no separate re-export step, no stale
  PNGs.
- Alternatives rejected: draw.io/diagrams.net (binary export, not diffable,
  stale by default), Excalidraw (binary export), Omnigraffle (macOS-only,
  binary), PlantUML everywhere (no native GitHub rendering in README), Mermaid
  everywhere (weak C4 support for documentation-site diagrams).

---

## DR-10: Hybrid data strategy — RBA dataset calibration + heuristic labeling

**Status:** Accepted
**Category:** Data Strategy

### Context

The fast ML scorer (XGBoost) needs training data, but no public labeled ATO
dataset exists with the right feature schema (device fingerprints, geographic
coordinates, session context, multi-event sequences). The scenario generator
produces synthetic events with `scenario_tag` labels, but those tags are
stripped before scoring — using them directly as training labels would be
circular.

### Decision

Adopt a three-part hybrid data strategy: (1) calibrate the scenario
generator's baseline behavior distributions from the DAS Group RBA dataset
(33M+ synthesized login events from 3.3M+ real users, ACM TOPS 2022), (2)
generate training events via the scenario generator using the full ATO Reasoner
event schema, (3) train XGBoost via a transparent, documented heuristic
labeling function that computes risk scores from engineered features (velocity
thresholds, novelty scores, impossible travel flags, IP reputation signals).

### Consequences

- The scenario generator's "normal" behavior is grounded in real-world login
  data (login velocity distributions, IP diversity patterns, user agent
  consistency, temporal cadence) rather than arbitrary parameters. This makes
  baseline events statistically plausible.
- The heuristic labeling function is transparent — committed to the repo,
  documented in this record, and explicitly framed as a reference
  implementation choice. The scorer's job is triage (sorting events into
  confidence bands: high-confidence safe, high-confidence risky, ambiguous) so
  that ambiguous events route to the LLM policy gate. It does not claim to
  detect real fraud.
- In production, the heuristic labeler would be replaced with labeled
  operational data. The training data source is pluggable by design — the
  architecture is the point, not the specific data source.
- The scenario generator is designed with clean interfaces (configuration-driven
  scenarios, pluggable distribution sources, well-documented event schema) so it
  could eventually be extracted as a standalone library.
- Alternatives rejected: random synthetic labels (meaningless model),
  scenario_tag as label (circular — tag is stripped before scoring), no scorer
  / rules-only triage (loses the hybrid ML+LLM architecture), collect real
  production data (impossible for a reference implementation), IEEE-CIS fraud
  dataset (transaction-level, not authentication-level — wrong domain).

### Scoping note (added 2026-04-29)

The "calibrate from the DAS Group RBA dataset" intent above was implemented
in the MVP as **qualitative calibration informed by the *published*
descriptive statistics in Wiefling et al. (ACM TOPS 2022)** — not by
ingesting the raw 31.3M-event dataset and fitting empirical distributions
to it. The scenario generator's archetype distributions and the trainer's
`_generate_sample` distributions are designed against the same archetypes
the paper characterizes; specific numbers (success/failure rates, device
class shares, OS shares, browser shares, login-frequency moments) are
cited from the paper's §4.1 and §4.3.

A polish-phase project that does the proper raw-dataset analysis (EDA →
distribution fitting → scenario re-tune → empirical validation) is scoped
in [`scenarios.md`](./scenarios.md). It's intentionally a separate
artifact rather than blocking the MVP — the architecture is the point at
this stage; the data-pipeline depth is a follow-on portfolio piece.

---

## DR-11: Two-layer core/ structure — framework protocols and domain implementations

**Status:** Accepted
**Category:** Pipeline & System Design

> **Implementation note:** When this decision was written, the domain layer was
> described as `core/account_takeover/`. It was subsequently placed in a
> top-level `reasoner/` package (e.g. `reasoner/account_takeover/`) to make the
> boundary unambiguous at the package level — domain types are not subpackages of
> the framework. All references below reflect the current `reasoner/` location.

### Context

DecisionLedger is a model-agnostic governed decision framework; the ATO Reasoner
is its reference implementation. Without an explicit structural boundary, domain-
specific types accumulate in the shared contract layer: `LoginEvent`,
`AuthMethod`, `GeoData`, the ATO-specific fields of `FeatureVector`
(`velocity_1min`, `ip_novelty`, `impossible_travel`), `Jurisdiction`, and
`RiskTier` all resided directly in `core/decision/` and `core/policy/`.

DR-8 enforced the infrastructure boundary (no database drivers or SDK imports
in `core/`). It did not address the framework/domain boundary. Each additional
reasoner domain — a marketing propensity system, a credit origination pipeline,
a vendor risk engine — would require embedding its own domain types alongside
ATO types in the same framework modules. The stated modularity guarantee would
not be reflected in the package structure.

### Decision

Separate into three layers:

**Framework contracts** (`core/decision/`, `core/policy/`, `core/eval/`) define
what the DecisionLedger framework guarantees for *any* reasoner domain:
- `Observation` — a Protocol specifying the minimum contract for any input
  event submitted to the framework: `event_id`, `entity_id`, and `timestamp`.
- `DecisionBundle`, `PolicyGateOutput`, `Citation`, `EvalReport` — generic
  audit, gate, and evaluation contracts that reference `Observation` rather
  than domain types.

**Domain layer** (`reasoner/account_takeover/`) contains ATO-specific types that
satisfy the framework protocols: `LoginEvent` (implements `Observation`),
`AtoFeatureVector`, `ScorerOutput`, `Jurisdiction`,
`RiskTier`. This is a top-level package — not a subdirectory of `core/` — so
that the boundary is unambiguous at the package level.

The import rule is one-directional and absolute: framework modules in `core/`
may not import from `reasoner/` or any other domain package. Domain packages
import from `core/`. `app/`, `eval/`, and `generator/` import from both layers
as needed.

A new reasoner domain is added as `reasoner/<domain>/` implementing `Observation`.
The `app/` layer wires the domain types into the pipeline.
No framework module changes are required.

### Consequences

- The import boundary is mechanically enforceable — an import linter rule on
  `core/decision/`, `core/policy/`, and `core/eval/` verifies that no framework
  module imports from a domain subpackage. Adding a second reasoner requires
  zero changes to the framework modules.
- Framework contracts (`Observation`, `DecisionBundle`) evolve
  independently of any domain. A change to how ATO features are computed does
  not touch the audit record contract.
- Existing imports across `app/`, `tests/`, `generator/`, and `eval/` must be
  updated: ATO-specific types now resolve from `reasoner/account_takeover/`, not
  `core/decision/` or `core/policy/`. This migration is mechanical and
  performed once at the time this boundary is introduced.
- Protocol-based structural subtyping (PEP 544) is used rather than abstract
  base classes. Domain types satisfy `Observation` implicitly —
  no inheritance required, no coupling to a base class.
- Alternatives rejected: flat structure with documentation only (boundary is
  not enforceable; documentation diverges from code); full TypeVar-parameterized
  generics throughout (`DecisionBundle[E, F]`) — correct in principle but adds
  abstraction overhead that obscures the pattern for a reference implementation;
  separate published packages per domain (premature packaging before the
  framework contracts have stabilized).

---

## DR-12: `entity_id` as the universal subject identity primitive

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

Every decision the framework produces is about a subject. In the ATO Reasoner
that subject is an account (`account_id`). In other reasoner domains the subject
might be a customer, a vendor, a borrower, or an organization — each with its
own domain-specific identifier.

Naming the framework's subject identity field `account_id` couples the contract
layer to the ATO domain and forces every other domain to map through account
semantics. It also introduces fragility: business keys change (account numbers
are reassigned, customer numbers migrate across systems), and a framework that
tracks decisions by mutable business key cannot guarantee stable auditability
across the lifetime of a subject. Cross-domain decision history — decisions
produced by different reasoners about the same real-world subject — cannot be
correlated without domain knowledge of which key to join on.

### Decision

The `Observation` protocol identifies subjects with `entity_id: UUID`. This is
the framework's universal subject identity primitive.

`entity_id` is a stable, platform-issued UUID assigned to any subject that
decisions are made about. It is not a business key. Domain-specific identifiers
(`account_id`, `customer_id`, `vendor_id`) are fields on domain types in
`core/<domain>/` — they are domain attributes, not the framework's identity
field. In `core/account_takeover/`, `LoginEvent.entity_id` carries the account under
evaluation; `account_id` is retained as a domain attribute for
application-layer use.

`UUID` is used rather than `str` to enforce generation at the platform boundary.
Populating `entity_id` from an arbitrary external string requires explicit
conversion, preventing accidental key conflation across domains.

### Consequences

- Decisions about any subject — account, customer, organization, vendor — are
  recorded and queryable through a single framework field. Decision history
  across reasoner domains is a framework-level query, not a domain-specific
  join.
- `entity_id` as UUID is stable across business key changes. Decisions recorded
  before and after a key reassignment remain correlated under the same identity.
- `entity_id` is the correct foundation for entity resolution: a real-world
  subject present in multiple domains (a person who is simultaneously a banking
  customer, a marketing contact, and a counterparty) can be resolved to a single
  `entity_id`, enabling cross-domain decision correlation without coupling the
  domains. Entity resolution is out of scope for MVP; establishing the identity
  field correctly at the framework contract level is the prerequisite.
- At production scale, `entity_id` becomes the natural join key for a master
  entity index — a UUID-keyed registry where entity type, domain identifiers,
  and lifecycle metadata are maintained. This is a well-established pattern in
  financial services and multi-domain platform systems. The framework contract
  is compatible with that architecture without requiring its implementation at
  MVP.
- Alternatives rejected: domain-specific identifier (e.g., `account_id`) at
  the framework contract level (couples framework to a single domain, breaks
  for any additional reasoner); `str` identifier (permits arbitrary formats, no
  enforcement of UUID semantics, invites conflation across domains); composite
  key (`entity_type: str` + `domain_id: str`) at the framework level (entity
  type resolution is a concern of the entity resolution layer, not the framework
  contract).
 
---

## DR-13: Dual-analyzer strategy for Elasticsearch policy-chunks index

**Status:** Accepted
**Category:** Retrieval & Infrastructure

### Context

The policy corpus contains two distinct query patterns that have opposing
requirements for text analysis:

1. **Natural language queries** — "what are the MFA requirements for high-value
   accounts?" — benefit from stemming and stopword removal. The `english`
   analyzer reduces "authenticating", "authenticated", and "authentication" to
   the same stem, improving recall across policy prose.

2. **Exact identifier queries** — "AAL2", "NIST 800-63B §5.2.3",
   "INTERNAL-RISK-v2.1" — require exact token matching. The `english` analyzer
   would stem or drop these tokens, causing misses on precise regulatory
   citations that the LLM policy gate relies on for citation accuracy.

A single analyzer cannot serve both patterns without sacrificing one.

### Decision

The `text` field in `infra/elasticsearch/policy-chunks.json` is mapped with
two analyzers via Elasticsearch multi-fields:

- `text` — `english` analyzer (stemming + stopword removal). Used for
  natural language retrieval queries constructed from scorer signals and
  event context.
- `text.keyword_search` — `standard` analyzer (whitespace tokenization, no
  stemming). Used when the retrieval query contains exact regulatory
  identifiers, version strings, or section references.

The retriever constructs multi-match queries across both fields. RRF fusion
then blends the BM25 scores from both paths with the dense pgvector results.
Neither analyzer path is authoritative — fusion handles the blend.

### Consequences

- Regulatory identifiers ("AAL2", "§5.2.3", "INTERNAL-RISK-v2.1") score
  correctly under `standard` analysis without being mangled by stemming.
- Natural language policy prose scores correctly under `english` analysis
  without requiring exact term matches.
- The retriever must be aware of both sub-fields and query them explicitly.
  A multi-match query across `["text", "text.keyword_search"]` with
  `type: best_fields` or `type: most_fields` is the standard pattern.
- Index size increases slightly due to storing two token streams per chunk.
  At the expected corpus size (60–80 chunks), this is negligible.
- Alternatives rejected: `english` only (fails on exact identifier queries,
  hurts citation accuracy eval dimension); `standard` only (misses stemmed
  prose matches, hurts recall on natural language queries); separate indices
  per analyzer (doubles infrastructure complexity, complicates RRF fusion).

---

## DR-14: `Observation` as domain-assembled output — `GateContext` and `FastPathRecord` as framework entry contracts

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

DecisionLedger claims to be model-agnostic. Its reference implementation (ATO
Reasoner) uses a two-stage architecture: a fast ML scorer triages events by
confidence band, and the LLM policy gate reasons over the ambiguous remainder.

An earlier draft of this record treated the scorer and feature computation as
framework pipeline stages — first-class components of DecisionLedger itself.
That framing was incorrect and violated the model-agnostic claim: for the
policy gate to build a prompt from `LoginEvent` + `AtoFeatureVector`, the gate
implementation would need to read domain-specific field names (`.geo.country`,
`.device_consistency_score`, `.auth_method`). A second domain would require a
second gate implementation. The gate would not be generic — it would be an ATO
gate that pretended to be generic.

The deeper issue: `ScorerOutput` embedded in `DecisionBundle` as a typed
top-level field meant the bundle contract was ATO-specific. A
retention domain, credit domain, or vendor risk domain could not produce a
valid bundle without satisfying ATO schema obligations.

### Decision

The `Observation` is what the upstream domain reasoner **produces** — not the
raw event that enters it. The domain layer (ATO Reasoner or any other) runs
its own feature computation, scoring, and context assembly. What it hands to
DecisionLedger is a complete, enriched `Observation` carrying two framework
contracts:

**1. `GateContext` (always required)**

`GateContext` carries everything the policy gate needs to reason:

```
prompt_template_id: str          # which versioned template to render
template_vars: dict[str, str]    # domain-rendered substitutions for the template
```

The domain is responsible for rendering `template_vars` from its own types —
converting `AtoFeatureVector.travel_speed_kmh` to `"1,340 km/h"`,
`LoginEvent.auth_method` to `"PASSKEY"`, etc. The gate loads the template,
substitutes vars, appends retrieved policy snippets, and calls the LLM. The
gate never reads a domain field name.

`GateContext` is populated on **every** observation, including fast-path ones.
This enables shadow evaluation: any fast-path decision can be retrospectively
sent through the gate offline to compare the scorer and gate decisions on the
same event. This is the infrastructure for the shadow eval capability described
in the polish roadmap.

**2. `FastPathRecord` (present when routing is `FAST_PATH_ALLOW` or `FAST_PATH_BLOCK`)**

`FastPathRecord` carries the provenance needed to replay a fast-path decision:

```
routing: GateRouting             # FAST_PATH_ALLOW or FAST_PATH_BLOCK
model_id: str                    # scorer model artifact (e.g., "xgb-v1.0.0")
risk_score: float                # [0.0, 1.0] score at inference time
top_signals: list[Signal]        # top-k SHAP-attributed signals
threshold_rule: str              # the rule that fired (e.g., "risk_score > 0.95")
feature_hash: str                # hash of the feature vector used for inference
```

When `routing` is `ROUTE_TO_GATE`, `fast_path_record` is `None` — the gate
path uses `gate_context` exclusively.

**The updated `Observation` protocol:**

```
event_id: str
entity_id: UUID
entity_type: str
timestamp: datetime
routing: GateRouting             # routing decision from the domain reasoner
fast_path_record: FastPathRecord | None
gate_context: GateContext
```

**The framework pipeline collapses to:**

```
Observation → ingest/dedup → [routing dispatch]
                               ├─ FAST_PATH_*  → enforce → bundle
                               └─ ROUTE_TO_GATE → retrieve → gate → enforce → bundle
```

Feature computation, scoring, and gate context assembly are domain concerns
that happen before the framework receives the Observation. In `app/`, the ATO
Reasoner pipeline is: ingest raw event → compute features → score → assemble
Observation → submit to framework.

### Consequences

- The policy gate is 100% generic. `app/policy_gate/` has no imports from
  `core/account_takeover/`. Adding a second domain requires zero changes to
  framework code — only a new prompt template and a domain context assembler.
- `DecisionBundle` loses typed top-level fields for `feature_snapshot`,
  `scorer_output`, and `scorer_model_version`. These move into the Observation
  (via `fast_path_record` and the rendered `gate_context.template_vars`). The
  bundle retains full auditability because it stores `raw_event: Observation`,
  which contains all domain-specific provenance.
- `ScorerOutput` moves from `core/decision/` to `core/account_takeover/` — it
  is an ATO domain type, not a framework contract. `Signal` and `FastPathRecord`
  remain in `core/decision/` as framework contracts.
- Shadow evaluation is architecturally available from day one: because
  `gate_context` is populated on fast-path observations, any historical batch
  can be replayed through the gate without re-running the domain pipeline.
- The `prompt_template_id` in `GateContext` replaces `prompt_version` as the
  gate's template reference. The bundle records it directly for audit queries.
  Prompt versioning semantics are unchanged (immutable once deployed, new
  version for any change, eval gate required before activation — DR-3 et al.).
- Alternatives rejected: making `scorer_output` Optional in the current bundle
  (would still couple non-ATO domains to the scorer schema); pre-rendering the
  full prompt in the domain layer (loses the framework's ability to inject
  freshly retrieved snippets at gate invocation time, breaking RAG).

---

## DR-15: D2 for all architecture diagrams

**Status:** Accepted
**Category:** Tooling & DX

### Context

DR-9 adopted Mermaid for README diagrams and PlantUML with the C4-PlantUML
library for container-level diagrams. In practice this produced a three-stage
rendering pipeline (structurizr-cli → apply-theme.py → plantuml) with three
distinct tool dependencies, a `workspace.dsl` source file, and two separate
diagram languages to maintain. Mermaid's C4 diagram type was also found to be
too limited for container and component diagrams once the system grew beyond a
few components — and its native GitHub rendering does not extend to C4 notation.

### Decision

Replace all diagram tooling with D2. Maintain four diagrams:

- `docs/design/diagrams/system-context.d2` — C4 Level 1: system context
- `docs/design/diagrams/containers.d2` — C4 Level 2: containers
- `docs/design/diagrams/components.d2` — C4 Level 3: pipeline components
- `docs/design/diagrams/reasoner-handoff.d2` — domain ↔ framework handoff (detail view)

Commit rendered SVGs to `docs/assets/diagrams/`. Render with `just diagrams`,
which loops over `docs/design/diagrams/*.d2`. The ELK layout engine is used for
all diagrams except `components.d2`, which uses dagre (handles flat pipeline
flows better than ELK for that diagram).

### Consequences

- A single `d2` binary replaces structurizr-cli, apply-theme.py, plantuml, and
  the C4-PlantUML library. `brew install d2` is the full setup.
- D2 source files are diffable text — diagram changes appear in the same PR as
  the architecture change they document.
- Rendered SVGs are committed: GitHub renders them inline in Markdown without
  any plugin or build configuration required.
- The four-diagram set (L1, L2, L3, handoff) covers the architecture at the
  right levels of detail for both portfolio readers and active implementation
  reference. L3 currently covers the Decision Pipeline container only; other
  containers are documented at L2.
- ELK layout handles nested containers (containers.d2, reasoner-handoff.d2)
  cleanly. The `components.d2` pipeline diagram uses dagre, which handles the
  flat component-flow layout better for that specific diagram.
- Alternatives rejected: Mermaid everywhere (weak C4 container/component support,
  no nested container styling); PlantUML everywhere (no native GitHub README
  rendering, heavy dependency chain); draw.io / Excalidraw (binary export, not
  diffable); Structurizr DSL with D2 export (still requires external tooling and
  a separate workspace model to maintain).

---

## DR-16: FastAPI entry point — summary DTO, sync handlers, and replay endpoint

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

Every pipeline component (ingestion through audit/replay) is implemented and
unit-tested. The final MVP deliverable is a FastAPI application that wires all
services into HTTP endpoints. Several design questions arose:

1. **Response shape.** Should `POST /api/v1/decisions` return the full
   `DecisionBundle` (~5–10 KB) or a summary DTO? The bundle is the canonical
   audit record — useful for debugging — but large for a synchronous response.

2. **Concurrency model.** All pipeline components (Redis, psycopg, Elasticsearch,
   OpenAI SDK) are synchronous. FastAPI supports both sync and async handlers.

3. **Replay exposure.** `BundleStore.replay()` already exists for deterministic
   enforcement replay. Should it be exposed via HTTP now, or deferred until a
   future CLI/eval tool?

4. **Model artifact bootstrap.** `AtoScorer` requires a pre-trained XGBoost
   model file at startup. Should the API auto-train if the file is missing?

### Decision

**Summary DTO with full bundle on a separate endpoint.**
`POST /api/v1/decisions` returns a `DecisionResponse` with `decision_id`,
`decision_action`, `enforcement_rule_applied`, `routing`, and `latency_ms`. The
full `DecisionBundle` is available at `GET /api/v1/decisions/{decision_id}`.
This separation keeps the primary response small and forces the GET endpoint to
exist — which a planned post-MVP reviewer UI will consume directly.

**Sync handlers.** All components are synchronous. FastAPI runs sync handlers in
a thread pool via `run_in_executor`, which is sufficient for the reference
implementation's concurrency needs. Migrating to async later requires no changes
to the component interfaces — only the handler functions and connection clients
would change.

**Replay endpoint included.** `POST /api/v1/decisions/{decision_id}/replay` is
included in the MVP API. The planned post-MVP reviewer application (likely
Streamlit or Django) will need HTTP access to both bundle retrieval and replay.
Building the endpoint now avoids giving the reviewer app direct database access
or requiring a separate CLI shim.

**Hard fail on missing model.** If `app/scorer/models/ato-v1.ubj` does not
exist at startup, the lifespan raises `FileNotFoundError` with a message
pointing to `make train`. Auto-training on startup was rejected: implicit
behavior during startup masks a missing prerequisite step and makes
first-run behavior unpredictable.

**Service lifecycle via lifespan context manager.** All infrastructure clients
(Redis, psycopg, Elasticsearch) and domain services (FeatureService, AtoScorer,
PolicyRetriever, PolicyGate, BundleStore) are constructed once at startup,
stored on `app.state`, and shared across requests. Connections are closed on
shutdown. This replaces the deprecated `@app.on_event("startup")` pattern.

### Consequences

- The API surface is four endpoints: `POST /decisions` (decide),
  `GET /decisions/{id}` (retrieve), `POST /decisions/{id}/replay` (replay),
  and `GET /health` (liveness). This is the minimum surface the reviewer UI
  needs.
- Switching to full-bundle POST response later is a one-line change (swap the
  return type and value). No pipeline logic changes required.
- Sync handlers mean request throughput is bounded by the thread pool size.
  For the reference implementation this is acceptable. Production deployments
  would either increase the thread pool or migrate to async clients.
- Hard fail on missing model means `docker compose up` + `uvicorn` is not
  sufficient — `make train` must be run first. This is documented in the
  runbook and the error message.
- Alternatives rejected: full-bundle response on POST (large payload, no
  forcing function for the GET endpoint); async handlers (premature — all
  clients are sync, and the interface migration cost is low); replay deferred
  (would require the reviewer app to implement its own replay path or get
  direct DB access); auto-train on startup (implicit, unpredictable startup
  time, masks missing prerequisite).

---

## DR-17: Model artifact fingerprint over model registry integration

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

`ReasonerContext.model_version` records a human-readable version string
(e.g., `"xgb-v1.0.0"`) in every DecisionBundle. This is a label, not a
verifiable reference. If the model file is overwritten, renamed, or a
version collision occurs in the artifact store, there is no way to prove
which exact binary produced a given prediction. In regulated environments,
model governance requires demonstrating artifact-level provenance.

Two approaches were considered: (1) integrate a model registry into the
framework (MLflow, SageMaker Model Registry, etc.), or (2) record a
content-addressable fingerprint and leave artifact management external.

### Decision

Add `model_artifact_sha256: str | None` to `ReasonerContext`. The domain
scorer computes `hashlib.sha256(model_bytes).hexdigest()` once at startup
and includes the digest in every `ReasonerContext` it produces. The
framework carries the value in the DecisionBundle without interpreting it.

To verify a bundle's model claim: hash the candidate artifact file and
compare to `model_artifact_sha256`. Matching digests prove the exact
binary. The field is `None` for API-based models (OpenAI, vendor black
boxes) where no local artifact exists.

Model artifact storage, versioning, discovery, and lifecycle management
remain the domain's responsibility or are delegated to an external model
registry (MLflow, W&B, SageMaker).

### Consequences

- Every bundle now carries a verifiable model fingerprint alongside the
  human-readable version string. Together they answer "which model?"
  (version) and "prove it" (hash).
- Zero infrastructure added to the framework. The fingerprint is a string
  field on a Pydantic model — stays within the `core/` boundary.
- The framework does not manage model artifacts, model registries, or
  model lifecycle. This is an explicit scope boundary, not an oversight.
  Production deployments should pair DecisionLedger with an external model
  registry.
- Alternatives rejected: framework-integrated model registry (duplicates
  MLflow/W&B/SageMaker functionality; adds infrastructure dependencies to
  a pure-contract layer; every model registry does this better than a
  custom implementation would).

---

## DR-18: Bundle is immutable; resolution is an append-only attempt log; CHALLENGE and HOLD are non-terminal

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

Earlier the framework carried a `ReviewPacket` field on every HOLD bundle —
a sub-record assembled by the enforcement layer that re-stated bundle data
(decision_id, entity_id, enforcement_rule, top SHAP signals) plus a
`priority` and a `hold_reason`. It was intended to feed a downstream review
queue, but that queue was never modeled. The corpus document
`int-hold-queue-v1.md` declared the bundle itself "the authoritative source
of truth," contradicting having a separate audit-side artifact that
duplicates the bundle.

More fundamentally, the framework had no path for the *post-decision*
lifecycle. A HOLD decision indicates the pipeline produced a non-terminal
action that requires further work (human review, MFA challenge outcome,
SLA-default expiry, automated outreach, etc.) — but there was no schema for
recording what actually happened. The same observation applies to
CHALLENGE: the gate emits CHALLENGE, the auth subsystem applies a step-up,
but the eventual pass/fail outcome had nowhere to live.

The action vocabulary also leaked the missing model. `final_action` on the
bundle implied terminality even when the action was HOLD or CHALLENGE,
which conflate "what the pipeline decided" with "what was ultimately
realized on the entity."

Three load-bearing constraints framed the redesign:

1. The framework's marquee guarantee (DR-13) is replay determinism: given a
   bundle, re-running enforcement against the cached gate output must
   reproduce the same action. Mutating the bundle post-decision (to attach
   resolution outcome, status, or reviewer rationale) breaks this contract
   structurally — the stored row no longer reflects "what the pipeline
   produced at decision time."
2. Resolution is heterogeneous and frequently multi-step. Real resolvers
   include analyst review, SLA-expiry defaults, MFA step-up, automated
   outreach, second-opinion models, external ticket systems, self-service
   verification, leadership overrides, and escalation chains. A
   single-record resolution slot cannot represent multi-step flows
   (escalation followed by review; outreach initiated then later confirmed)
   without either overwriting state or re-inventing an attempt-list inside
   a single field.
3. Decision actions partition naturally into terminal {ALLOW, BLOCK} and
   non-terminal {CHALLENGE, HOLD} (MECE). Only non-terminal actions need
   resolution; terminal actions are realized by the pipeline itself.

### Decision

**Vocabulary.** Rename `final_action` → `decision_action` everywhere it
appears (Pydantic models, JSONB keys, SQL columns, indexes, structured-log
fields). Introduce a precise three-term action vocabulary:

- `decision_action` — the action the pipeline produced at decision time.
  Lives on the `DecisionBundle`. Immutable.
- `resolution_action` — the action a resolver produced. Lives on
  `ResolutionAttempt` rows. Immutable per row.
- `realized_action` — what was ultimately taken on the entity. Computed
  from the bundle plus the resolution attempt log. Never stored. For
  terminal `decision_action` it equals `decision_action`; otherwise it is
  the first terminal `resolution_action` in the attempt chain (or `None`
  if pending).

**Bundle is immutable.** The `decision_bundles` row is INSERT-only and
never updated post-decision. `ReviewPacket` is removed from the framework
entirely; the bundle itself carries every field a reviewer needs (entity,
signals, retrieval results, gate output, override log).

**Resolution is an append-only attempt log.** A new `core.resolution`
module defines `ResolverKind`, `ResolutionStatus`, and `ResolutionAttempt`
(Pydantic, frozen). A new `decision_resolution_attempts` table — INSERT-only,
keyed by `(decision_id, sequence)` — stores attempt rows. Multi-step flows
are expressed as multiple rows; new states are new attempts, never row
mutations. `app.audit.resolution_journal.ResolutionJournal` mirrors the
patterns of `BundleStore` (idempotent schema init, structured logging) and
exposes `record_attempt`, `load_attempts`, `realized_action`, and
`resolution_status`. The `realized_action` fold walks attempts in sequence
order and returns the first terminal `resolution_action`.

**Resolver-kind catalog.** `ResolverKind` enumerates the framework's
resolver vocabulary. MVP implements only `HUMAN` and `SLA_DEFAULT`.
`STEP_UP_AUTH`, `AUTOMATED_OUTREACH`, `SECOND_OPINION`, `EXTERNAL_TICKET`,
`SELF_SERVICE`, `OVERRIDE`, and `ESCALATION` are wireframed extension
points — the enum members exist so consumers can begin writing attempts of
those kinds; per-kind `evidence` payload conventions are post-MVP work.

**CHALLENGE and HOLD share the same lifecycle.** The MECE partition
(terminal vs non-terminal) makes CHALLENGE not a magical "the auth layer
handles it" hand-wave — it's a non-terminal `decision_action` whose
realized outcome is captured by a `STEP_UP_AUTH` (or other) resolution
attempt. The same `realized_action` fold applies uniformly to both.

**Priority lives outside the framework.** Review-queue priority is a
function of operational concerns (account tier, campaign correlation,
business policy) that change independently of the framework's data model.
Priority is computed by downstream queues from `enforcement_rule_applied`
(or other fields) at consumption time. It is not stored on the bundle and
not in `core.resolution`.

### Consequences

- **Replay determinism is preserved structurally.** The `decision_bundles`
  row is byte-stable post-write. Replay produces the original
  `decision_action` because no field of the bundle has changed. Resolution
  outcomes live in a separate, additive table that does not affect replay.
- **The action vocabulary is now MECE.** Terminal {ALLOW, BLOCK} and
  non-terminal {CHALLENGE, HOLD} cover the complete action space. CHALLENGE
  and HOLD share lifecycle machinery. Code that reads `decision_action`
  always gets the pipeline's verdict; code that needs the realized outcome
  calls `realized_action()` explicitly. The distinction is forced into the
  type system rather than implied.
- **Multi-step resolution is native.** Escalation, outreach-initiated +
  outreach-confirmed, expired-then-retried, and bulk/coordinated
  resolutions all express naturally as ordered attempt rows. No nested
  list-of-events inside a mutable resolution slot.
- **Heterogeneous resolvers without schema churn.** New resolver kinds add
  an enum value and a per-kind `evidence` payload convention. The
  framework schema does not change.
- **Storage migration.** The bundle JSONB serializer drops `review_packet`
  and renames `final_action` → `decision_action`. Old rows in dev
  databases will not deserialize forward; pre-production MVP accepts a
  drop-and-rebuild. Production deployments would require either a JSONB
  rewrite migration or a tolerant deserializer; both are out of MVP
  scope.
- **Resolver implementations live outside the framework.** The framework
  defines the data model (ResolutionAttempt) and persistence
  (ResolutionJournal). The mechanics of a review queue (UI, SLA timer,
  reviewer auth) and the integration of each resolver kind (auth
  subsystem for STEP_UP_AUTH, ticket-system webhooks for EXTERNAL_TICKET,
  outreach automation for AUTOMATED_OUTREACH, etc.) are downstream
  concerns. Each post-MVP resolver kind warrants its own DR documenting
  the integration contract.
- **Alternatives rejected.** (a) *Mutable resolution slot on the bundle.*
  Single record per decision with a `resolution: HoldResolution | None`
  field that downstream queues fill in. Simplest reads; breaks bundle
  immutability and replay-determinism contract; cannot natively express
  multi-step resolvers. (b) *Linked child decision bundle.* Each
  resolution recorded as a new full `DecisionBundle` with a
  `parent_decision_id`. Maximally symmetric but heavyweight — every
  resolution row carries the full retrieval/gate/observation surface,
  most of which is empty for resolutions. (c) *Single-record resolution
  table.* One row per decision in a separate `decision_resolutions`
  table. Preserves immutability but cannot represent multi-step
  resolution without re-introducing nested-list state inside a row.


---

## DR-19: GateInput / GateOutput contracts on the bundle (typed gate-invocation records)

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

Through DR-15 (gate-routing decoupling), DR-18 (decision_action / immutable
bundle / resolution log), and an interim slice that renamed
`final_action → decision_action` and added Optional gate-text fields, the
bundle's domain-leaking surfaces had been incrementally peeled back. What
remained was the gate-layer surface itself: six scattered top-level fields
on `DecisionBundle` (`gate_input_snapshot`, `gate_input_text`,
`gate_response`, `gate_output`, `gate_model_version`, `gate_token_cost`)
each baking in an LLM/retrieval-shaped assumption.

Each of those fields encoded an implicit claim about the gate. A rule
engine has no `gate_model_version` (it has a `rules_version`); a typed gate
that consumes structured input has no `gate_input_text`; a non-LLM gate
has no `gate_token_cost`. New gate types had no contract telling them what
they MUST capture — leaving them either to populate `None` for fields that
don't apply (silent absence) or to lie via misleading values
(`gate_model_version="rule-engine-v1.2"`).

### Decision

Replace the six scattered gate-layer fields on `DecisionBundle` with two
typed Pydantic contracts:

- `gate_input: GateInput | None` — the input artifacts of a gate invocation
- `gate_output: GateOutput | None` — the output artifacts

Both `None` on the fast path. Both populated when the gate ran (whether or
not validation succeeded). Defined in `core/gate/input.py` and
`core/gate/output.py`. The `GateOutput.verdict: GateVerdict | None` field
holds the framework-consumable typed verdict (formerly `PolicyGateOutput`,
renamed to `GateVerdict` to disambiguate from the new `GateOutput`
wrapper).

Each contract has a small set of universal required fields and an
`extras: dict[str, Any]` extension surface. Per-gate-type Pydantic
sub-records serialize through `extras` via `model_dump(mode="json")`;
readers reconstruct with `Model.model_validate(extras["key"])`. The
reference LLM policy gate stores `PromptSnapshot` in
`gate_input.extras["prompt_snapshot"]` and writes `model_version`,
`prompt_template_id`, `corpus_version`, etc. into `gate_input.config`.

`TokenCost` moves from `core/bundle.py` into `core/gate/output.py`,
co-located with the only thing that references it. Re-exported from
`core/gate/__init__.py`.

`corpus_version` flows orchestrator → `PolicyGate.evaluate(corpus_version=...)`.
The gate writes it into `gate_input.config["corpus_version"]`. The reasoner
layer is not involved — `corpus_version` is infrastructure-side metadata
and stays out of `reasoner/` per the framework's domain-boundary discipline.

### Consequences

- **Bundle surface stays small as gate types proliferate.** Adding a
  rule-engine gate, a second-opinion-model gate, or a webhook gate does
  not require new fields on `DecisionBundle`. Each gate populates
  `GateInput.config` and `GateInput.extras` with whatever it considers
  part of its effective configuration; the framework persists the dicts
  without interpretation.
- **Self-documenting contract for new gate implementations.** A new gate
  reads `GateInput` / `GateOutput` and knows what it must populate. The
  base classes' Pydantic schemas plus their docstrings ARE the contract.
- **Schema-failure invariant becomes structurally explicit.** The
  three-state contract is now type-encoded:
  - `gate_output is None` — gate not invoked (fast path).
  - `gate_output is not None and gate_output.verdict is not None` — gate
    ran, verdict valid.
  - `gate_output is not None and gate_output.verdict is None` — gate ran,
    verdict failed validation; `gate_output.response_text` carries
    forensic evidence; enforcement routes to HOLD via tier1.
- **Replay determinism contract holds.** Replay calls
  `enforcement.resolve(..., bundle.gate_output.verdict, ...)` and asserts
  the result matches `bundle.decision_action`. Same guarantee as before;
  one extra `.verdict` dereference. Independent of gate implementation.
- **Forward-only storage migration.** The bundle JSONB serializer now
  emits two top-level keys (`gate_input`, `gate_output`) instead of six.
  Old rows in dev databases will not deserialize forward; same precedent
  as DR-18 — drop and rebuild the dev DB. Production deployments would
  require either a JSONB-rewrite migration or a tolerant deserializer;
  out of MVP scope.
- **Slice-C remainder.** `PolicySnippet` (still in `core/bundle.py`) is
  the largest remaining domain-leaking type name. `PromptSnapshot` lives
  in `core/gate/prompt.py` and is still framework-level even though it's
  LLM-specific (now stored inside `gate_input.extras` rather than as a
  bundle field, so its presence at the framework level is reduced). Both
  are deferred to a future slice.
- **Alternatives rejected.** (a) *Single dict for both sides.*
  `gate_input: dict[str, Any] | None` and `gate_output: dict[str, Any] |
  None`. Maximum flexibility but loses Pydantic validation on the
  framework-consumable verdict — and the verdict IS the load-bearing
  contract that enforcement reads. (b) *Discriminated union per gate
  kind.* `class LLMGateInput(GateInput)` etc. with Pydantic
  discriminator. Highest type safety but requires the framework to know
  every gate kind ahead of time, defeating the extensibility goal. The
  base-contract + extras-dict pattern preserves typed validation where it
  matters (universal fields + verdict) and stays open for gate-specific
  data without framework changes. (c) *Keep the six scattered fields and
  just add `gate_id` + `gate_config_snapshot`.* Considered as an
  incremental step but rejected: it would have left the LLM-specific
  fields on the bundle in perpetuity and not addressed the contract
  question for new gate types.

---

## DR-20: Universal gate contracts; per-gate-type subpackages; closed discriminated union for MVP

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

DR-19 collapsed the bundle's six scattered gate-layer fields into typed
`GateInput` / `GateOutput` contracts and renamed `PolicyGateOutput` →
`GateVerdict`. Right structural move; stopped short of true abstraction:

- The renamed `GateVerdict` still carried `rationale: str` and
  `citations: list[Citation]` — both LLM-policy-explanation patterns. A
  rule-engine gate has no rationale-as-prose; an ML classifier has no
  citation list. Universal in name only.
- `GateInput.input_text` and `GateInput.config: dict[str, Any]` were
  LLM-shaped (text input, unconstrained config dict). A typed gate
  consuming structured features doesn't have either.
- `GateInput.extras` and `GateOutput.extras` were `dict[str, Any]`
  escape hatches. Each gate type was documented to put a typed Pydantic
  sub-record there, but the framework couldn't enforce it. The
  "Pydantic-typed all the way" claim broke at this layer.
- All policy-gate-specific types (`PolicyGateOutput`-now-`GateVerdict`,
  `Citation`, `PromptSnapshot`, `PromptTemplate`, `PromptRegistry`)
  lived at `core/gate/` top level mixed with the universal contracts —
  the namespace didn't reflect the layering.

### Decision

**Universal vs concrete split.** Universal contracts at `core/gate/`
(``GateInput``, ``GateOutput``, ``GateVerdict``) carry only what the
framework's enforcement layer reads:

- `GateVerdict` — `gate_id`, `permitted_actions`, `required_controls`,
  `confidence`, `escalate_to_human`, `escalation_reason`. No rationale.
  No citations. Only the action/control/confidence machinery
  enforcement.resolve() actually consumes.
- `GateOutput` — `gate_id`, `verdict`. No `response_text`, no
  `token_cost`, no `extras`.
- `GateInput` — `gate_id` only. No `config`, no `input_text`, no
  `extras`.

Concrete subclasses live in per-gate-type subpackages
(``core/gate/policy/`` for the LLM-backed policy gate; future kinds get
sibling subpackages like ``core/gate/rule/``). Each subclass narrows
``gate_id`` to a Pydantic ``Literal[...]`` for discriminated-union
deserialization and adds typed top-level fields for its
implementation-specific artifacts:

- `PolicyGateVerdict(GateVerdict)` adds `rationale` and `citations`.
- `PolicyGateOutput(GateOutput)` adds `response_text` and `token_cost`.
- `PolicyGateInput(GateInput)` adds `model_version`,
  `prompt_template_id`, `prompt_template_version`, `corpus_version`,
  `rendered_prompt`, `prompt_snapshot`, `template_vars`.

`Citation`, `PromptSnapshot`, `PromptTemplate`, `PromptRegistry`,
`TokenCost` move to ``core/gate/policy/`` since they were always
LLM-policy-gate-specific.

**App-layer mirror.** ``app/policy_gate/`` → ``app/gate/policy/``
mirroring ``core/gate/policy/``. New gate kinds get sibling
``app/gate/<kind>/`` subpackages.

**Discriminated union for deserialization.** ``DecisionBundle`` types
its fields as the universal base (``gate_input: GateInput | None``,
``gate_output: GateOutput | None``); subclass instances assigned at
construction are preserved at runtime. JSONB deserialization in
``app/audit/store.py`` picks the concrete subclass — for MVP a
single-variant union (``PolicyGateInput`` / ``PolicyGateOutput``);
adding a future gate kind is a one-line union extension. Open registry
plumbing for third-party gates is deferred to post-MVP.

**Discriminator field is `gate_id`, not a separate `kind` or `name`.**
The ``gate_id`` already on ``GateContext`` and on the universal base
contracts doubles as the Pydantic discriminator via Literal narrowing
in subclasses. No redundant near-synonym field.

**No `extras: dict[str, Any]` at the framework level.** Removed, not
deprecated. Per-gate-type subclasses must declare typed Pydantic fields
for any artifact they want captured.

### Consequences

- **Pydantic-typed all the way.** No `dict[str, Any]` escape hatches at
  any level. Every artifact a gate captures is a typed Pydantic field
  on either the universal base (for truly universal data) or a concrete
  subclass (for kind-specific data). Validation happens at construction
  time; the IDE tells implementers what fields are required.
- **Truly framework-level gate-type-agnosticism.** The universal
  ``GateVerdict`` carries only what enforcement reads. A rule-engine
  gate that produces ``RuleGateVerdict(GateVerdict)`` with a typed
  rule-trace field is now structurally first-class — same rights as the
  policy gate, no carve-outs, no abstract dict.
- **Self-documenting per-gate contracts.** ``PolicyGateInput``'s
  Pydantic schema IS the documentation for what the LLM gate captures.
  Same for ``PolicyGateOutput`` and ``PolicyGateVerdict``. No
  out-of-band conventions.
- **Dependency inversion at the package boundary.** Framework code
  (``core.bundle``, ``core.enforcement``-equivalent contracts) depends
  only on universal base contracts. Per-gate-type code lives in its own
  subpackage and depends on the universal base. ``core.bundle`` does
  not import ``PolicyGateInput`` or any concrete gate type.
- **Subpackage layout reflects layering.**
  ``core/gate/<kind>/{input,output,verdict}.py`` plus ``app/gate/<kind>/``
  for the runtime orchestrator. Pattern is predictable for new kinds.
- **MVP closed-union acceptable cost.** Adding a new gate kind requires
  adding the variant to a single union in ``app/audit/store.py``. For
  2–5 gate kinds this is fine. Open registry can land post-MVP without
  breaking the universal contracts.
- **Forward-only storage migration**, same precedent as DR-18 / DR-19.
  Old JSONB rows with ``gate_input.config`` / ``gate_output.extras``
  shapes won't deserialize forward — drop and rebuild the dev DB.
- **Alternatives rejected.**
  (a) *Single dict for both sides* (``gate_input: dict[str, Any]``,
  ``gate_output: dict[str, Any]``). Maximum flexibility but loses
  Pydantic validation on the framework-consumable verdict — and the
  verdict IS the load-bearing contract that enforcement reads.
  (b) *Discriminated union per gate kind without a universal base.*
  ``GateInput = Annotated[PolicyGateInput | RuleGateInput | ...,
  Field(discriminator="gate_id")]`` with no shared base class. Tighter
  type at deserialization but the framework loses a type to depend on
  for "any gate's input." The base-class pattern with Literal narrowing
  gives both: subclass instances preserved at runtime AND a universal
  type to depend on at the framework layer.
  (c) *Separate `kind` discriminator field*. The semantic distinction
  (kind = category, name = identifier) has merit, but ``gate_id`` is
  already the established identifier in ``GateContext`` and on the
  universal contracts. Adding a near-synonymous ``kind`` field
  duplicates concepts. Resolved by making ``gate_id`` itself the
  discriminator via Literal narrowing.
  (d) *Open registry for third-party gates*. Premature for MVP. The
  closed-union pattern lets the framework know its supported gate kinds
  at deliberate-PR cadence; runtime registration via a registry can be
  added later without breaking the universal contracts.

---

## DR-21: SOLID cleanup — package surface, ResolutionAttempt subclassing, RetrievedSnippet rename, EnforcementDecision relocation

**Status:** Accepted
**Category:** Pipeline & System Design

### Context

A SOLID audit of `core/` after DR-20 surfaced four issues:

1. **Cross-subpackage import.** `core/observation/observation.py` imported
   `GateRoute` from `core.gate` — the only hard violation of the project's
   own stated rule (in `core/__init__.py`'s docstring) that subpackages
   never import from siblings.
2. **Concrete-type leakage at the abstraction's `__init__`.** `core/gate/__init__.py`
   re-exported eight `PolicyGate*` / `Citation` / `PromptSnapshot` /
   `PromptTemplate` / `PromptRegistry` / `TokenCost` types from
   `core.gate.policy` "for ergonomic convenience." Mixed abstraction
   signal at the framework's package surface — reading
   `from core.gate import PolicyGateInput` next to
   `from core.gate import GateInput` told consumers concrete and abstract
   types live at the same tier.
3. **OCP-pattern inconsistency in `ResolutionAttempt`.** DR-20 eliminated
   `extras: dict[str, Any]` from gate contracts. `ResolutionAttempt`
   kept `evidence: dict[str, Any]` — the same soft-typing escape hatch.
4. **Naming-honesty issues.** `PolicySnippet` (in `core/bundle.py`) was
   the framework's universal retrieved-corpus-chunk type with an
   LLM-policy-flavored name. `EnforcementDecision` (also in
   `core/bundle.py`) was the enforcement layer's intermediate output
   bundled with the `DecisionBundle` audit record — single-responsibility
   nudge.

### Decision

**Slice 1 — package surface cleanup:**
- `GateRoute` moves from `core/gate/routes.py` to top-level
  `core/routes.py`. Both `core/observation/` and `core/gate/` import
  from there; neither crosses subpackage boundaries.
- `core/gate/__init__.py` exposes only universal contracts (`GateInput`,
  `GateOutput`, `GateVerdict`, plus `DocumentType` and `PolicyDocument`
  as framework-corpus metadata). Concrete LLM-policy types are accessed
  via `from core.gate.policy import ...` explicitly.
- `core/bundle.py` reaches into subpackages via their `__init__.py`
  interfaces: `from core.gate import GateInput, GateOutput`,
  `from core.observation import Observation`. Matches the project's
  stated dependency rule.

**Slice 2 — ResolutionAttempt discriminator pattern:**
- `core/resolution.py` becomes `core/resolution/` (subpackage) with
  the universal base in `attempt.py` and per-resolver-kind subclasses
  in sibling modules (`human.py`, `sla_default.py`).
- Universal `ResolutionAttempt` drops the `evidence: dict[str, Any]`
  field. `HumanResolutionAttempt` adds typed `reviewer_role` and
  `reference_ticket_id` fields. `SlaDefaultResolutionAttempt` adds
  typed `account_tier` and `sla_window_seconds` fields.
- Discriminator is `resolver_kind` (Pydantic Literal narrowing in each
  subclass). `app/audit/resolution_journal.py` uses a closed
  discriminated union for deserialization — same pattern as
  `app/audit/store.py` for gate contracts.
- Persistence: the `decision_resolution_attempts` SQL table gains a
  `payload jsonb not null` column for the typed-subclass payload. The
  legacy `evidence jsonb` column is preserved nullable for
  backward-compatible reads of pre-DR-21 rows; new writes leave it null
  and a follow-up migration drops it.
- Other `ResolverKind` enum members (`STEP_UP_AUTH`,
  `AUTOMATED_OUTREACH`, `SECOND_OPINION`, `EXTERNAL_TICKET`,
  `SELF_SERVICE`, `OVERRIDE`, `ESCALATION`) remain enumerated for
  forward declaration; their subclasses land when implemented post-MVP.

**Slice 3 — naming + relocation:**
- `PolicySnippet` becomes `RetrievedSnippet`, relocated from
  `core/bundle.py` to `core/snippet.py`. Its `policy_id` field renames
  to `document_id`. The DB column in `policy_chunks` keeps its name
  (the schema is policy-corpus-specific by design); the retriever maps
  `row["policy_id"]` → `RetrievedSnippet.document_id` at the boundary.
- `EnforcementDecision` moves from `core/bundle.py` to
  `core/enforcement.py`. Consumers (`app/audit/bundle.py`,
  `app/audit/store.py`, `app/enforcement/resolver.py`) update import
  paths.

### Consequences

- **Zero cross-subpackage imports inside `core/`.** The dependency
  graph now matches the project's own stated rule. New subpackages
  (e.g., a future `core/gate/rule/` for a rule-engine gate) inherit
  the discipline by default.
- **Framework-surface honesty.** Reading `from core.gate import ...`
  in a consumer reveals only the universal contracts. Concrete
  LLM-policy-gate types require an explicit
  `from core.gate.policy import ...` — making the abstraction layer
  a structural signal, not a documentation claim.
- **Pydantic-typed all the way through `core/`.** No `dict[str, Any]`
  escape hatches at the framework level. Both gate contracts (DR-20)
  and resolution attempts (DR-21) follow the same subclass-with-
  discriminator pattern — pattern consistency makes the next gate
  kind or resolver kind a known move.
- **`core/` file layout matches conceptual layout.** Each persisted
  framework artifact lives in its own module:
  `bundle.py` (DecisionBundle), `enforcement.py` (EnforcementDecision),
  `snippet.py` (RetrievedSnippet), `resolution/` (ResolutionAttempt
  + subclasses), plus the subpackages `gate/`, `observation/`, `eval/`
  for self-contained functional concerns. SRP-clean.
- **Domain-neutral names match domain-neutral semantics.**
  `RetrievedSnippet.document_id` no longer claims "this is policy"
  when it might equally be retrieved from a knowledge base, source
  corpus, or rule index by a non-policy-LLM gate.
- **Forward-only storage migration**, same precedent as DR-18/19/20.
  The dev DB drops and rebuilds; production deployments need a JSONB
  rewrite migration for the `payload` column on
  `decision_resolution_attempts`. Out of MVP scope.
- **Alternatives rejected.**
  (a) *Keep `evidence: dict[str, Any]` on `ResolutionAttempt`.* Soft
  typing for "extensibility" — but inconsistent with DR-20's stance on
  gate contracts. The cost of the typed-subclass pattern (one new
  module per resolver kind) is the same blast radius as the gate-kind
  pattern, which we accepted.
  (b) *Half-rename `PolicySnippet` → `RetrievedSnippet` while keeping
  `policy_id` field.* Worse than no rename — type name and field name
  drift apart. Full rename including the field forces a coherent
  vocabulary at every consumer.
  (c) *Re-export `EnforcementDecision` from `core/__init__.py` for
  ergonomic convenience.* Same pattern we just removed from
  `core/gate/__init__.py`. Consumers import from the canonical module.


## DR-22: Eval harness — RAGAS in, openevals out, custom SDK-agnostic LLM-judge primitive

**Status:** Accepted
**Category:** Evaluation & Tooling

### Context

The 5-dimension eval harness defined by `core/eval/metrics.py` (retrieval,
faithfulness, consistency, citation, robustness) needed two LLM-touching
components: a RAG-style faithfulness scorer (claim decomposition +
entailment against retrieved contexts) and an LLM-as-judge primitive used
by faithfulness and citation. The market offers two off-the-shelf options:

- **RAGAS** — paper-backed metric implementations (faithfulness via
  claim-decomposition + per-claim entailment, context precision/recall,
  answer relevance, etc.).
- **openevals** (LangChain) — a `create_llm_as_judge` factory composing
  prompt template + Pydantic-schema output + chat-client call.

We had to choose what to bring in, what to skip, and how to defend the
choices to a senior reviewer evaluating the project for engineering
judgment as much as functionality.

### Decision

**RAGAS — accepted as a dev dependency.** Its faithfulness metric is the
genuinely non-trivial work the eval harness needs. Reproducing
claim-decomposition + per-claim entailment in-house wastes time on a
solved problem and produces a less-defensible eval story. RAGAS lives
behind an `eval/clients/ragas.py` adapter implementing the
`RagasFaithfulnessScorer` protocol — the dimension code (`eval/dimensions/
faithfulness.py`) never imports RAGAS directly. Imports are deferred to
first-call to keep the unit-test layer fast.

**openevals — rejected.** The decision rests on architecture, not
dep-tree weight. Reasons:

1. **Architectural consistency.** `app/gate/policy/` already uses raw
   `OpenAI` SDK with structured outputs (`response_format=PydanticModel`).
   The eval harness mirrors that style with `eval/clients/openai.py:
   OpenAIJudgeClient`. One LLM-call abstraction in the codebase, not two.
2. **Small, legible surface area.** The custom judge primitive is ~80
   LOC of explicit Python (Pydantic schema + Protocol + thin adapter)
   versus a factory call buried in a third-party library. Easier to
   reason about, easier to extend with judge-specific tweaks (calibration,
   retry policies, prompt iteration).
3. **Multi-model swap.** The `JudgeClient` Protocol gives us first-class
   SDK-agnosticism. Adding an Anthropic, DeepSeek, or local-endpoint
   backend means writing one new file under `eval/clients/`. openevals'
   LangChain coupling does not provide that boundary as cleanly.
4. **Telemetry and error-handling control.** With our own client we
   choose how parse failures and refusals surface. Using openevals means
   inheriting LangChain's exception conventions.

**The dep-tree-weight argument was rejected as a defense.** RAGAS already
pulls `langchain`, `langchain-core`, `langchain-community`,
`langchain-classic`, `langchain-openai`, `langchain-text-splitters`,
`langsmith`, and `langgraph` into dev deps transitively. The marginal
cost of openevals would be near zero in dependency terms. The asymmetry
that justifies different decisions is *what each library implements*, not
how heavy it is.

### Implementation

- **`JudgeClient` Protocol** (`eval/judge.py`) — SDK-agnostic
  structured-completion contract: `complete_structured(*, system, user,
  response_format) -> response_format`. Concrete clients live in
  `eval/clients/`.
- **`OpenAIJudgeClient`** (`eval/clients/openai.py`) — wraps
  `AsyncOpenAI.beta.chat.completions.parse` to satisfy the protocol.
  This is the only module under `eval/` permitted to import an OpenAI
  SDK class.
- **`RagasFaithfulnessScorer` Protocol** (`eval/dimensions/
  faithfulness.py`) — same SDK-agnostic pattern; `eval/clients/ragas.py`
  is the concrete adapter.
- **Judge prompts** in YAML at `eval/prompts/judges/` — versioned,
  snapshottable, mirrors the `app/gate/policy/prompt_registry.py`
  pattern.
- **Dimensions** (`eval/dimensions/{retrieval,faithfulness,consistency,
  citation,robustness}.py`) consume only the protocols. None imports an
  SDK class. Verified by grep.

### Consequences

- **Eval harness is multi-model from day one.** Swapping the judge LLM
  is one new file under `eval/clients/`; nothing in the dimension or
  runner code changes.
- **Architecture is consistent across `app/` and `eval/`.** Both go
  through raw OpenAI SDK with structured outputs. Reviewers see one
  pattern, not two.
- **RAGAS' LangChain footprint is encapsulated** in
  `eval/clients/ragas.py`. The dimension code never touches LangChain.
- **The defense to a senior reviewer is principled.** *RAGAS earns
  its coupling because it implements paper-backed algorithms; openevals
  doesn't — its primary offering is a wrapper the OpenAI SDK already
  exposes. Different decisions, different reasons.*
- **Tradeoff accepted.** Custom code means we own correctness for the
  judge primitive. The size (~80 LOC) keeps that ownership cheap; if
  the judge primitive grows complex, the openevals reconsideration is
  warranted and gets its own DR.
- **Alternatives rejected.**
  (a) *Skip RAGAS too; build claim-decomposition + entailment in-house.*
  Wastes effort reproducing solved-problem metric algorithms; weakens
  the eval story.
  (b) *Use openevals because LangChain is "already paid for" via RAGAS.*
  Dep-tree weight is not the load-bearing argument — architecture is.
  Adding a second LLM-call abstraction for a wrapper we don't need
  fails the consistency test regardless of marginal dep cost.
  (c) *Build the judge primitive but skip the `JudgeClient` Protocol.*
  Couples the dimensions to `AsyncOpenAI` directly. Loses the
  multi-model swap property — and the cleanest boundary the protocol
  provides — for nothing in return.


## DR-23: Unified `LLMClient` adapter — one provider boundary for gate and judge

**Status:** Accepted (supersedes DR-22's `JudgeClient`)
**Category:** Architecture / Provider Abstraction

### Context

DR-22 introduced the `JudgeClient` Protocol so the eval harness could
swap LLM providers (OpenAI, Anthropic, local) by writing one new
`eval/clients/<provider>.py` file. The policy gate (`app/gate/policy/
gate.py`) — built earlier — was never given an analogous abstraction:
its constructor took a concrete `OpenAI` SDK client and called
`client.chat.completions.create(...)` inline.

The asymmetry was indefensible. The gate is the *production critical
path*: provider outages or pricing changes block the decision pipeline,
not the offline eval. Model capability is shifting rapidly enough
(Sonnet 4.6 → Opus 4.7; Gemini 2.0 Flash matching 4o-quality at 1/30th
the price) that A/B-testing gate models cannot require an architectural
rewrite. A senior reviewer would (correctly) ask why the lower-stakes
component had the abstraction and the higher-stakes one didn't.

Reading the existing code surfaced a sharper observation: the
`JudgeClient` Protocol —
`complete_structured(*, system, user, response_format) -> response_format`
— is **not judge-specific.** It's a generic LLM-call primitive that
happened to be named for its first use site. The right architectural
move is unification, not a parallel `LLMClient`-for-the-gate beside the
existing `JudgeClient`-for-the-judge.

### Decision

**One adapter, two domain facades.** Introduce `core/llm/client.py:
LLMClient` as the single SDK-agnostic LLM-call primitive. Both
`PolicyGate` (decision path) and the eval harness's `llm_judge`
function (offline) compose it. Concrete adapters live in `app/llm/`
— one per provider:

- `core/llm/client.py` — `LLMClient` Protocol (async),
  `CompletionResult[T]` (parsed + usage + latency),
  `TokenUsage` (neutral primitive).
- `app/llm/openai.py` — `OpenAILLMClient` using
  `client.beta.chat.completions.parse(...)` (strict JSON Schema).
- `app/llm/anthropic.py` — `AnthropicLLMClient` using forced tool use
  for structured output.
- `app/llm/_pricing.py` — model→price lookup; adapters populate
  `TokenUsage.cost_usd` from it.

`JudgeClient` is retired. `eval/judge.py:llm_judge` consumes
`LLMClient` directly. `eval/clients/openai.py` is deleted —
`OpenAIJudgeClient`'s body lives in `app/llm/openai.py:OpenAILLMClient`.
Two concrete adapters (OpenAI + Anthropic) ship together to prove the
protocol generalizes across provider response shapes (OpenAI's strict
parse vs Anthropic's tool-use translation).

**Async cascade.** `LLMClient.complete_structured` is async-only.
`PolicyGate.evaluate`, `app.decide.execute_pipeline`, and the FastAPI
`create_decision` route all become async. `PipelineDriver` drops the
`asyncio.to_thread` wrapping around `execute_pipeline`. FastAPI is
async-native; LLM calls are I/O-bound; the previous mixed sync/async
boundary was a build-order artifact.

**RAGAS adapter unchanged.** `eval/clients/ragas.py` uses LangChain
wrappers internally, not our `LLMClient`. DR-22 explicitly accepted
that asymmetry to encapsulate the RAGAS↔LangChain coupling. Routing
RAGAS through `LLMClient` is larger separate work and is not part of
this decision.

### Implementation

- **Universal contract** in `core/llm/`. Zero infrastructure imports;
  preserves the `core/` boundary rule.
- **Concrete adapters** in `app/llm/`. The only modules in the project
  permitted to import an LLM SDK class. Verified by grep at the
  boundary — `app/gate/`, `eval/`, `core/` contain zero `import
  openai` or `import anthropic` references.
- **Cost computation** centralized in `app/llm/_pricing.py` — adapters
  populate `TokenUsage.cost_usd` from the table; `cost_usd is None`
  for unknown model ids.
- **`PolicyGate.evaluate`** translates the `CompletionResult.usage`
  into the existing `core.gate.policy.output.TokenCost` for
  `PolicyGateOutput`. Audit-side semantics unchanged.

### Consequences

- **Symmetry restored.** Gate and judge consume the same primitive. A
  reviewer cannot ask "why one and not the other" because the answer
  is "both, through one adapter."
- **Multi-provider testing is now a constructor swap.** Instantiate
  `OpenAILLMClient` for one and `AnthropicLLMClient` for the other to
  reduce in-family bias between gated decisions and their LLM-as-judge
  receipts.
- **Strict structured output upgrade for the gate.** The legacy
  `response_format={"type": "json_object"}` path is replaced by
  SDK-enforced JSON Schema conformance via
  `client.beta.chat.completions.parse(...)`. Schema-validation
  failures surface at the SDK boundary instead of as silent
  deserialization errors at the gate's manual `_parse_verdict()` step.
- **Async cascade through the decision path.** Lower latency under
  concurrent load and aligns with FastAPI's native model. The
  `PipelineDriver` no longer needs `asyncio.to_thread` for the gate
  call.
- **DR-22's `JudgeClient` is retired.** `JudgePromptRegistry`,
  `JudgeOutput`, and `JudgePromptTemplate` remain — those are
  judge-domain abstractions distinct from the LLM-call primitive.
- **Alternatives rejected.**
  (a) *Add a parallel `GateLLMClient` Protocol beside the existing
  `JudgeClient`.* Two interfaces for the same call shape — fails the
  "one LLM-call abstraction in the codebase, not two" test that DR-22
  itself invoked.
  (b) *Defer to a polish item.* Closing the asymmetry before Step 4's
  baseline capture means the committed receipts reflect the
  architecture we ship. Capturing baselines on the old plumbing then
  refactoring later would require re-capture.
  (c) *Ship OpenAI adapter only; defer Anthropic.* Single concrete is
  not a proof of generalization. The protocol claim is more credible
  with two adapters than with the design alone.
