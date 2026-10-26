<p align="center">
  <img src="docs/assets/decision-ledger-hero/decision-ledger-hero-universal.png" alt="DecisionLedger Hero" width="850">
</p>

<p align="center">
  <a href="https://github.com/charleskelley/decision-ledger/actions/workflows/ci.yaml"><img src="https://github.com/charleskelley/decision-ledger/actions/workflows/ci.yaml/badge.svg" alt="CI"></a>
  <a href="https://github.com/charleskelley/decision-ledger/actions/workflows/integration.yaml"><img src="https://github.com/charleskelley/decision-ledger/actions/workflows/integration.yaml/badge.svg" alt="Integration"></a>
  <a href="https://github.com/charleskelley/decision-ledger/actions/workflows/eval.yaml"><img src="https://github.com/charleskelley/decision-ledger/actions/workflows/eval.yaml/badge.svg" alt="Eval"></a>
  <a href="https://github.com/charleskelley/decision-ledger/actions/workflows/docs.yaml"><img src="https://github.com/charleskelley/decision-ledger/actions/workflows/docs.yaml/badge.svg" alt="Docs"></a>
</p>

<p align="center">
  <strong>Live docs:</strong> <a href="https://charleskelley.github.io/decision-ledger">charleskelley.github.io/decision-ledger</a>
</p>

AI outputs are probabilistic. The accountability required when those outputs
affect customers is not. Every production decision needs grounded reasoning,
a policy basis, a citable rationale, and a reproducible audit trail. And every
change to the system that produces those decisions must prove it has not
regressed.

> **This project is designed to demonstrate the engineering principles required 
> to make AI decisioning production-safe:**
>
> * **The reasoning layer is pluggable -** each domain is a reasoner (ML, LLM,
> rules, or any combination inside it) that submits its decisions to the
> ledger over HTTP or in process; the framework never imports one.
> * **The control layers are constant -** versioned artifacts, evaluation-gated
> releases, and immutable state capture.
> * **The decisions recorded are auditable by design -** deterministic
> enforcement, context and evidence bundling, and full audit replay.
>
> DecisionLedger demonstrates the engineering required when AI outputs affect
> real customers: not just model decision quality, but release control,
> traceability, replayability, and safe operationalization in high-stakes
> environments.

***AI reasons. Policy enforces. Everything is replayable.***

### Current MVP Status

| # | Launch criterion                                                                       | Status |
|---|----------------------------------------------------------------------------------------|--------|
| 1 | `make check` — lint, sqlfluff, typecheck, and unit tests all pass                      | ✅     |
| 2 | `make test-smoke` — all 8 ATO scenarios pass end-to-end                                | ✅     |
| 3 | `make eval` — 5D evaluation harness produces a passing baseline report                 | ✅ re-run 2026-10-11 on `ato-v3` |
| 4 | All four GitHub Actions workflows (CI, Integration, Eval, Docs) green on `main`       | ✅ green after the DR-26 merge |
| 5 | Versioned baseline eval report ([`outputs/eval/eval-report-v2.json`](outputs/eval/eval-report-v2.json)) committed + linked | ✅     |

Every row reads ✅: the MVP shipped as `mvp-v1.0`.

> **Note:** `eval-report-v2.json` (2026-10-11) is the first baseline after
> DR-26: it measures the `ato-v3` template (system prompt relocated into the
> template, rendered text unchanged) and the `reasoner_id`-filtered retrieval
> path. All five dimensions passed. `eval-report-v1.json` (2026-05-06,
> `ato-v2`) is kept as history. Evaluation has since been split between the
> framework's harness and the RAG policy gate's own harness (below); no
> baseline has been committed under that split yet.

### The Production Problem

Governed AI decision systems introduce operational failure modes that classical
MLOps does not fully cover, especially when retrieval and LLM-based reasoning
enter the path.

Models, prompts, retrieval indexes, and policy corpora all influence behavior.
A change to any of them is a deployment event with behavioral consequences.
Retrieval behavior can drift as the corpus changes without touching application
code. Rationale quality can fail silently even when aggregate model metrics look
acceptable. And when decisions affect customers, probabilistic reasoning still
has to terminate in deterministic accountability.

DecisionLedger treats these as first-class engineering problems with explicit,
testable controls. 

### Where This Sits

This section states the project's positioning as of October 2026, from a
survey of adjacent products. It is a reading of the market, not a measured
result.

Every system that makes an automated decision about a customer logs it its own
way, if at all. Rules engines audit their own verdicts, customer data platforms
hold profiles, lineage tools trace data, observability tools trace LLM calls,
and governance tools inventory models. None of them is built to answer "show
me every automated decision about this customer, and replay the one they are
disputing." DecisionLedger is a reference implementation of a decision system
of record that can, with a gating service for the cases that need one.

| Category | Examples | What they hold | What the ledger adds |
|---|---|---|---|
| Decision logging inside one engine | FICO Platform, SAS Intelligent Decisioning, Pega, IBM ODM | An audit of that engine's own decisions | One bundle schema any system can write, across engines and vendors, with replay |
| Customer data platforms | Segment, Treasure Data, Adobe Real-Time CDP | Customer profiles and events | The decisions about those customers, with their evidence, policy basis, and enforcement outcome |
| Data and ML lineage | OpenLineage / Marquez, MLflow, feature stores | Which data and models produced an artifact | The individual decisions about individual subjects |
| Model risk inventories | IBM watsonx.governance, Credo AI, Holistic AI | Which models exist, their risk tier, their validation status | The decision-level record a dispute or an examination asks for |
| LLM observability and evaluation | LangSmith, Langfuse, Arize Phoenix, Braintrust, W&B Weave, Galileo | Traces and scores of LLM calls | A decision contract, deterministic enforcement, a subject-centric ledger, and replay |
| Policy runtimes and agent-decision logs (2026 entrants) | PolicyLLM, Policy-as-Skill, AEL SDK | Policy-checked verdicts; agent-scoped decision logs with replay | A cross-system record of every decision about a subject; such tools can write to it as reasoners or gates |

What is distinctive, in order:

1. **One Decision Bundle schema for every automated decision**, whatever
   produced it: the submission as received, the subject's identity, each
   gate's input, output, and evidence snapshot, and the enforcement outcome.
   Immutable.
2. **A subject-centric ledger with replay.** Every decision about one subject,
   across reasoners, in one timeline, each replayable from its bundle: the
   record a complaint handler, a dispute analyst, or an examiner asks for.
3. **A gating service for the ambiguous cases.** Gates run on a decision
   before it is final; the reference RAG policy gate retrieves from a
   versioned policy corpus and asks an LLM for a schema-valid verdict with
   citations and confidence, and deterministic enforcement decides after it.
4. **Eval-gated release.** Prompts, indexes, and corpora are deployment
   events, and an evaluation harness gates every release candidate.

The first two are the product; the last two are why it can be trusted.
Reasoners and gates are clients of the ledger, not parts of it.

### Engineering Principles Demonstrated

The system is organized around the operating discipline required to make AI
decisioning production-safe:

- **Control-plane architecture** — separate reasoning from enforcement so AI
  contributes to decisions without owning final action semantics.
- **Artifact-aware release management** — prompts, retrieval indexes, policy
  corpora, and model versions are treated as deployable artifacts with explicit
  regression risk.
- **Evaluation-gated change management** — release decisions depend on behavior,
  not just offline model metrics.
- **Replayable, auditable decisioning** — every decision can be reconstructed
  from versioned inputs, retrieved evidence, intermediate state, and final
  enforcement outcome.
- **Design for high-scrutiny environments** — patterns built for domains where
  traceability, policy grounding, and operational safety matter as much as raw
  model performance.

### Decision Pipeline

<p align="center">
  <img src="docs/assets/decision-ledger-overview.png" alt="Decision Pipeline Overview" width="850">
</p>

> **Note:** This overview predates DR-27 to DR-32 and draws the earlier,
> conceptual flow. In the ledger as built, evidence retrieval runs inside the
> RAG policy gate, a plugin; the ML "fast path" is a decision the reasoner
> records under one of its registered bypass rules (DR-30); the LLM path is
> that gate, which the ledger runs when the reasoner asks for a gate; the
> human path is a held decision an operator resolves through the ledger's
> resolution journal; and the ledger resolves each decision's subject before
> any gate runs (DR-32). The [submission flow](docs/assets/diagrams/reasoner-handoff.svg)
> and [the pipeline](docs/design/pipeline.md) show the current flow.

The **framework** is `core/` (contracts), `app/` (the ledger runtime), and
`eval/` (the evaluation harness); its only infrastructure is Postgres. Three
roles work with it, and the framework imports none of them:

- **Reasoner** — a package or service that decides, or proposes, an action
  about a subject and submits it to the ledger, in process or over HTTP. The
  framework never imports one. Account Takeover (`reasoners/account_takeover`)
  is the reference reasoner; `reasoners/example` is a minimal one.
- **Gate** — a plugin the ledger runs on a decision (kind `rules`, `lookup`,
  or `llm`), bound to a reasoner as mandatory (it runs on every decision) or
  advisory (it runs only when the reasoner asks for a gate). The ledger
  enforces each gate's output and combines them. The reference gates are the
  rules gate (`gates/rules`) and the RAG policy gate (`gates/policy_rag`).
- **Identity resolver** — a plugin the enterprise supplies that maps the
  subject a reasoner names to the enterprise's own subject. The framework
  implements no identity resolution; it ships only the passthrough default.

A reasoner uses one of two verbs. It **records** a decision it made, citing
one of its registered bypass rules (the ledger checks the rule against the
reasoner's evidence, and the mandatory gates still run), or it asks the
ledger to **gate** one (the mandatory gates run, then the advisory gates). The ledger resolves every
decision's subject through the resolver the enterprise supplies and records
the mapping and its provenance on the bundle: the enterprise's resolver
decides who the subject is, and the passthrough records the subject the
reasoner names, unresolved.

**Reasoner** — The reasoner owns its events, ingestion, features, and scoring,
and builds the `Submission` (the wire contract): its evidence, the subject it
scored, and one gate context for every gate it binds. Account Takeover scores
login events with XGBoost and records the clear cases under its two score-band
rules (`allow-band`, `block-band`); it asks the ledger to gate the rest.

**Gates** — The RAG policy gate, a workspace member and not part of the
framework, runs hybrid retrieval (dense embeddings and BM25, fused and
reranked by a cross-encoder) over the reasoner's own versioned policy corpus,
then asks the LLM for a schema-valid verdict with citations and confidence.
Any gate can be swapped for another implementation of the same gate contract.

**Ledger** — The ledger validates the submission, resolves its subject, runs
the mandatory gates and then (on a gated decision) the advisory gates, and
enforces each gate's output deterministically. It combines them: the most
restrictive action wins, under a few decision-level rules (on a gated
decision about a novel entity, the advisory gates' verdicts are held for
review), and a gate that fails, times out,
or returns an unusable output holds the decision. It then writes a
**Decision Bundle**, a complete, immutable record of the submission, the
subject, every gate's evidence and output, and the enforcement outcome. Replay
re-runs enforcement from the bundle and the snapshot of the registration in
effect when the decision was made, without calling any gate or model again.

### Core Guarantees

| Guarantee                      | Mechanism                                                         |
|--------------------------------|-------------------------------------------------------------------|
| No uncontrolled AI actions     | Reasoners and gates propose; deterministic enforcement decides; a failing gate holds |
| No ungrounded decisions        | A record cites a registered rule that replay re-checks; a gated decision carries the gate's cited evidence |
| No silent artifact regressions | Every versioned artifact passes behavioral eval before production |
| No silent index drift          | Each decision bound to the RAG gate records its corpus version; the gate's retrieval eval gates a new corpus |
| Full reproducibility           | Every decision replayable from its Decision Bundle                |

 
### The 5D Evaluation Framework

The evaluation harnesses are the control mechanism that keeps artifact changes
from becoming silent behavioral regressions. Five evaluation dimensions run on
every release candidate — including prompt changes and retrieval index updates,
not just model artifacts. Each harness measures what it owns: the framework's
harness drives each reasoner's own pipeline, and the RAG policy gate's harness
measures the gate on the datasets each reasoner ships for it.

| Dimension                   | Harness              | What It Catches                                       |
|-----------------------------|----------------------|-------------------------------------------------------|
| **Retrieval Quality**       | RAG policy gate      | Wrong policy evidence reaching the reasoning layer    |
| **Generation Faithfulness** | RAG policy gate      | Rationale not grounded in retrieved evidence          |
| **Decision Consistency**    | Framework            | Action instability across equivalent event orderings  |
| **Citation Accuracy**       | RAG policy gate      | Superficial or irrelevant policy citations            |
| **Adversarial Robustness**  | Framework            | Injection attempts, schema violations, novel patterns |

The framework's harness also runs a report-only **shadow** dimension: it
re-decides a sample of recorded decisions with the reasoner's advisory gates
and compares the outcomes. No release candidate passes unless all five
dimensions clear defined thresholds. Quality and integration gates run on every
push to `main`; the eval harnesses run on demand via GitHub Actions
(`workflow_dispatch`), with the passing baseline report committed as the
release receipt.
 

---

### Quickstart

```bash
uv sync                        # Install the framework + every workspace reasoner, gate, and integration
cp .env.example .env           # Then paste your OPENAI_API_KEY and ANTHROPIC_API_KEY
docker compose up -d           # Start Postgres (the framework's), Elasticsearch (the RAG gate's), Redis (ATO's)
make prepare-reasoners         # Load every reasoner's policy corpus into the RAG gate; train ATO's scorer if missing
make install-hooks             # Install the pre-commit and pre-push hooks (gitleaks + ruff)

# Run the ledger API (port 8000) with a key for ATO and an operator key
DECISIONLEDGER_API_KEYS=ato-reasoner:ato-key DECISIONLEDGER_OPERATOR_KEY=op-key make serve

# In a second shell: run ATO as its own service (port 8001), submitting to the ledger
ATO_LEDGER_URL=http://localhost:8000 ATO_LEDGER_KEY=ato-key make serve-account_takeover

# In a third shell: send ATO one synthetic login event, then read, replay, and verify the decision
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 1 --dry-run \
  | tail -1 > /tmp/ato-event.json
curl -s -X POST localhost:8001/api/v1/ato/decisions \
  -H 'content-type: application/json' --data @/tmp/ato-event.json
curl -s localhost:8000/api/v1/decisions/<decision_id> -H 'X-DecisionLedger-Key: ato-key'
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/replay -H 'X-DecisionLedger-Key: ato-key'
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/verify -H 'X-DecisionLedger-Key: ato-key'

# Run the smoke tests (includes the 8 ATO scenarios; calls OpenAI)
make test-smoke

# Run the eval harnesses on ATO (slow, paid): the framework's, then the RAG gate's
make eval-account_takeover
make eval-gate-policy_rag REASONER=ato-reasoner
```

The ledger refuses to start until every corpus a served registration binds
is loaded: with `DECISIONLEDGER_REASONERS` blank it serves every installed
reasoner — ATO, the example reasoner, the batch and daily mocks, which bind
only the rules gate (the daily mock binds it mandatory), and the stream and
text mocks — whose policy bindings name `ato-policy`, `example-policy`,
`mock-stream-practices`, and the text mock's two corpora,
`mock-text-regulatory` and `mock-text-practice`, and `make
prepare-reasoners` loads them all.

All infrastructure runs locally via `docker compose up`; no cloud account is
required. Two provider keys are needed: `OPENAI_API_KEY` for the RAG policy
gate and `ANTHROPIC_API_KEY` for that gate's faithfulness and citation judges.
See [`docs/operations/secrets.md`](docs/operations/secrets.md) for full
secrets-handling guidance across local, CI, and production deployment, and
[`docs/development/runbook-local.md`](docs/development/runbook-local.md) for
the full local walk-through (the example reasoner, batches, timelines, the MCP
server, and the conformance suite).


---

### Documentation

The full documentation site covers architecture, implementation details, and
design rationale:

**[charleskelley.github.io/decision-ledger →](https://charleskelley.github.io/decision-ledger)**

| Section                                                                  | What You'll Find                                             |
|--------------------------------------------------------------------------|--------------------------------------------------------------|
| [Architecture](https://charleskelley.github.io/decision-ledger/design/architecture/)     | C4 diagrams, component dependencies, directory layout        |
| [Pipeline](https://charleskelley.github.io/decision-ledger/design/pipeline/)             | The decision order, latency budget, fail-closed paths        |
| [Identity](https://charleskelley.github.io/decision-ledger/design/identity/)             | The identity contract, the passthrough, entity journals, timelines |
| [Gates](https://charleskelley.github.io/decision-ledger/design/gates/)                   | The gate contract, gate sets, and how to implement a gate    |
| [Evaluation](https://charleskelley.github.io/decision-ledger/design/evaluation/)         | Framework and gate harnesses, metrics, thresholds            |
| [Submission contract](https://charleskelley.github.io/decision-ledger/design/reasoners/reasoner-handoff/) | The wire submission, record and gate, and the new-reasoner checklist |
| [ATO Data](https://charleskelley.github.io/decision-ledger/design/reasoners/account-takeover/data/) | ATO login-event schema, scorer output, Decision Bundle structure |
| [Design Decisions](https://charleskelley.github.io/decision-ledger/design/decisions/)    | DR-1 through DR-32 — the significant choices and why         |
| [Infrastructure](https://charleskelley.github.io/decision-ledger/design/infrastructure/) | Docker Compose stack, `decisionledger` schema, environment   |
| [Scenarios](https://charleskelley.github.io/decision-ledger/design/reasoners/account-takeover/scenarios/) | Eight named event patterns and what each exercises           |

 
---

### Transferability Across Domains

DecisionLedger demonstrates the pattern using account takeover detection as the
reference domain. The corpus, feature set, action space, and reasoning layer all
change across domains. The underlying decision-system architecture stays the
same even as the domain changes: a new domain is a new reasoner, a workspace
package or its own service, with no framework edits. Account Takeover is the
only domain reasoner that exists today (`reasoners/example` is a minimal
example); the rows below are illustrations.

| Domain                    | Corpus                                  | Action Space                               |
|---------------------------|-----------------------------------------|--------------------------------------------|
| Clinical decision support | Treatment guidelines, formularies       | Recommend · Escalate · Override            |
| Content moderation        | Platform policies, jurisdiction rules   | Allow · Remove · Escalate                  |
| Loan underwriting         | Credit policy, fair lending regulations | Approve · Decline · Counter                |
| Regulatory change impact  | Filed regulations, internal policies    | No action · Update required · Legal review |

 
---

### Workplan & Status

<p align="center">
  <img src="docs/assets/decision-ledger-roadmap.png" alt="DecisionLedger Roadmap" width="850">
</p>

The MVP scope shipped (tag `mvp-v1.0`); DR-26 to DR-32 have since turned the
framework into a decision system of record with pluggable reasoners, gates,
and identity resolvers. The table below distinguishes the launch scope from
post-launch polish and intentional exclusions.

| Phase | Scope | Status |
|---|---|---|
| **MVP** | End-to-end decision path for all 8 scenarios; 5D evaluation harness in CI with thresholds; Decision Bundle construction and deterministic replay; hybrid retrieval with cross-encoder reranking; scenario generator calibrated from real-world distributions | **Shipped (`mvp-v1.0`)** |
| **Polish** | Drift monitoring; shadow and canary rollout infrastructure; human review queue with pre-populated review packets; AWS deployment via Terraform; confidence calibration analysis; technical blog post | **Planned after MVP** |
| **Intentionally out of scope** | UI/dashboard; full Kafka implementation; LLM fine-tuning; real user data; production SLA hardening | **Not part of launch target** |
 
---

<p align="center">
  <sub>
    <a href="https://charleskelley.github.io/decision-ledger">Documentation</a>&nbsp;&nbsp;·&nbsp;&nbsp;<a href="https://charleskelley.github.io/decision-ledger/design/decisions/">Design Decisions</a>&nbsp;&nbsp;·&nbsp;&nbsp;<a href="https://charleskelley.github.io/decision-ledger/design/evaluation/">Evaluation Framework</a>
  </sub>
</p>
 
