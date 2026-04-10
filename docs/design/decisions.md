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

**Current head: DR-15.** The next record is DR-16.

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
