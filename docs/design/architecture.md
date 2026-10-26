# Architecture

DecisionLedger is a decision system of record with a gating service (DR-27).
Reasoners decide, or propose, actions about subjects and submit them to the
ledger. The ledger validates each submission, establishes its subject through
the enterprise's identity resolver, runs the decision's gates, enforces
deterministically, and writes an immutable Decision Bundle that it can replay.
Reasoners, gates, and identity resolvers are plugins or clients; the framework
imports none of them. The diagrams follow the C4 model, from the system's
context down to the ledger's components.

## System Diagram - Level 1

![C4 system context: domain reasoners (Account Takeover shipped, others illustrative) submit decisions to DecisionLedger to record or to gate and get back a decision id and the final action; DecisionLedger runs its gates, and for ambiguous cases its RAG policy gate asks an LLM service for a recommended verdict while deterministic enforcement decides the final action; the enterprise's identity resolver tells DecisionLedger who each decision's subject is; the decision analyst reads a subject's decisions, replays them, and resolves held ones](../assets/diagrams/system-context.svg)

Every reasoner submits through the same contract, the wire `Submission`, in
one of two modes: it *records* a decision it made, citing one of its
registered bypass rules, or asks the ledger to *gate* one (DR-30). The ledger
answers with a receipt that carries the decision id and the final action.
Account Takeover (ATO) is the shipped reference reasoner; the other reasoners
drawn are illustrative. The LLM service only recommends a verdict to the RAG
policy gate; deterministic enforcement inside the ledger makes the call. The
enterprise's identity resolver (its master data or crosswalk) maps the subject
a reasoner scored to the enterprise's subject, and the ledger records that
mapping on every bundle (DR-32; see [Identity](identity.md)). The decision
analyst reads a subject's decisions, replays them, and resolves held ones.

## Containers Diagram - Level 2

![C4 containers: inside the DecisionLedger boundary, the Ledger API (a FastAPI app that loads gate plugins and the identity resolver plugin in process) and PostgreSQL (schema decisionledger: bundles, registrations, resolution attempts, the entity journal and membership); the RAG policy gate owns Elasticsearch and its policy_rag schema on the same Postgres server; outside the boundary, the Account Takeover service (port 8001, with its own Redis) submits over HTTP or runs the ledger in its own process, agent reasoners call the ledger through the MCP server, the RAG gate calls the LLM service, the enterprise identity resolver is an installed plugin, and the decision analyst uses the API](../assets/diagrams/containers.svg)

The ledger runs as one process, the Ledger API (`make serve`:
`uv run uvicorn --factory app.main:create_app`, port 8000). At startup
`create_app` discovers the installed gate plugins (`decisionledger.gates`
entry points) and reasoner plugins (`decisionledger.reasoners`, filtered by
`DECISIONLEDGER_REASONERS`), and its lifespan builds the shared services once
(`app/bootstrap.py`), including the identity resolver the deployment names
(`DECISIONLEDGER_IDENTITY_RESOLVER`, loaded from the
`decisionledger.identity_resolvers` group; the built-in passthrough when
none). The lifespan also builds the live registry: the plugins'
registrations, the registration file's (`DECISIONLEDGER_REGISTRATIONS`),
and those written over the API, one source per reasoner, each admitted by
the gates it binds (DR-33; see [Registration](registration.md)). It mounts
no reasoner router and runs no reasoner code (DR-31).

The framework's only infrastructure is PostgreSQL. Its `decisionledger`
schema holds the decision bundles, the replay logs, the resolution attempts,
the registration snapshots and the registrations written over the API, the
identity journal and its membership projection, and the evidence escrow
(the content gates relied on, content-addressed; DR-34) (`app/audit/sql/`). The RAG policy gate (`gates/policy_rag`) owns
its own stores: Elasticsearch for BM25 search (its `compose.yaml`) and the
`policy_rag` schema with the pgvector extension, which its own DDL creates on
the same Postgres server. That is why the root `docker-compose.yml` runs the
`pgvector/pgvector` image; the framework's DDL creates no extension (DR-28).

Outside the boundary:

- **Account Takeover** runs as its own FastAPI service
  (`make serve-account_takeover`, port 8001) with its own Redis, from
  `reasoners/account_takeover/compose.yaml`. It submits to the Ledger API
  over HTTP (`HttpClient`, when `ATO_LEDGER_URL` is set) or runs the ledger in
  its own process (`LocalClient`, the default), writing to the same Postgres.
- **The MCP server** (`decisionledger-mcp`, stdio) lets agent reasoners call
  the ledger over HTTP as tools (`integrations/mcp`; reference:
  [`decisionledger_mcp`](../reference/integrations/mcp.md)).
- **The LLM service** is called by the RAG policy gate only.
- **The enterprise identity resolver** is an installed plugin
  (`decisionledger.identity_resolvers`); none ships with this repository.
- **The decision analyst** uses the ledger's routes (lookup, replay, verify,
  the subject timeline, resolutions).

The offline tools (each reasoner's scorer trainer, scenario generator, and
eval adapter; the RAG gate's corpus loader; the two eval harnesses) run on
demand and are not drawn. The RAG gate's retrieval depends on one of them:
its reference provider reads the corpora its corpus loader writes, under
the corpus ids reasoners' bindings name (`make build-policy-index` loads
every corpus the members ship).

## Component Diagram - Level 3 (Ledger API)

![C4 components of the Ledger API: the routes and the key ring call the operations (create_decision, record_batch, lookup, replay, verify, resolve, the entity timeline, register_reasoner), which call decide and submit: intake validation, the identity step (resolver or passthrough), the gate set, gate invocation (mandatory gates concurrently, then advisory), gate-level enforcement, the decision-level rules and the combinator, and the bundle store write to PostgreSQL; gate invocation calls the gate plugins, among them the RAG policy gate, which retrieves through a retrieval provider from each corpus a binding names, and the plugins put what they relied on in the evidence escrow; side paths are the live registry (registrations from three sources: plugins, the registration file, and the operator's PUT over the API; every version snapshotted), replay (re-enforcing stored gate outputs under the registration snapshot), verify (asking each gate that ran how its evidence stands, and checking which artifacts the escrow holds), and the entity journal and timelines; LocalClient enters at the operations](../assets/diagrams/components.svg)

Level 3 zooms into the Ledger API. A route authenticates its caller by the
`X-DecisionLedger-Key` header (`app/auth.py`) and calls an *operation*
(`app/api/`): `create_decision`, `record_batch`, `lookup_decision`,
`replay_decision`, `verify_decision`, `resolve_decision`, `entity_decisions`,
`register_reasoner` (the operator's `PUT` of a registration), and the
read-only resources. `LocalClient` (`app/client.py`) calls the same
operations in process as the trusted principal, so an HTTP request and an
in-process call run identical code. Intake reads the live registry
(`app/reasoner_registry.py`): `register_reasoner` admits a registration as
startup admits one, and the next decision reads the new version
([Registration](registration.md)). A decision goes through `app/submit.py`:
intake validation, the identity step (`app/identity/`), the gate set, gate
invocation (`app.gates.invoke_gate`; mandatory gates concurrently, then
advisory ones), gate-level enforcement, the decision-level rules and the
combinator (`app/enforcement/resolver.py`), and the bundle write
(`app/audit/store.py`). Replay re-enforces a bundle's stored gate outputs
under the snapshot of the registration version it cites, without invoking a
gate. Verify (`verify_decision`) asks each gate that ran how its evidence
references stand (`app.gates.verify_evidence`), beside the store's own check
of the evidence escrow; it decides nothing again (DR-34).
[Pipeline](pipeline.md) walks the same path step by step.

---

## Directory Layout

The repository is a uv workspace. The root project, `decision-ledger`, is the
framework: `core/`, `app/`, and `eval/`. Everything else that runs is a
workspace member with its own `pyproject.toml` and import package (members:
`reasoners/*`, `gates/*`, `integrations/*`), discovered at runtime through
entry points (DR-26, DR-28) or acting as a client of the ledger (DR-31).

```
decision-ledger/
├── pyproject.toml            Framework project + workspace root; dependency groups reasoners, gates, integrations compose the checkout
├── docker-compose.yml        Postgres (pgvector image); `include:` pulls in ATO's Redis and the RAG gate's Elasticsearch
├── Makefile                  Framework targets; `-include` of every reasoners/*/reasoner.mk and gates/*/gate.mk
├── justfile                  Convenience recipes (`just diagrams`)
│
├── core/                     Framework contracts: Pydantic models, protocols, pure functions; no infrastructure
│   ├── actions.py                DecisionAction (ALLOW, CHALLENGE, HOLD, BLOCK)
│   ├── submission.py             Submission (wire schema v2), BypassCitation, BypassCheck, intake and bypass-rule checks
│   ├── identity.py               The identity contract (NativeSubject, ResolvedIdentity, IdentityResolver, journal types); a leaf of core
│   ├── bundle.py                 DecisionBundle (schema version 2) and GateRecord
│   ├── gate_request.py           GateRequest: what the ledger hands a gate
│   ├── enforcement.py            EnforcementDecision
│   ├── exceptions.py             Framework exceptions, including LedgerError (status + stable code)
│   ├── observation/              ReasonerContext, GateContext, and the registration (GateBinding, BypassRule, ReasonerRegistration)
│   ├── gate/                     GateInput, the open GateOutput and EvidenceRef, GateVerdict, the predicate language, the combinator
│   ├── resolution/               Append-only resolution attempts (human, SLA default)
│   ├── llm/                      SDK-agnostic LLMClient protocol (DR-23)
│   └── eval/                     Eval dimension and metric contracts, EvalReport
│
├── app/                      Framework runtime: imports core/, owns Postgres, names no reasoner or gate
│   ├── main.py                   create_app(): discovers plugins, builds services in the lifespan, serves /health and /api/v1
│   ├── api/                      The ledger's routes and operations; wire models; RFC 9457 problem documents
│   ├── auth.py                   API keys to principals (reasoner, operator, trusted in-process)
│   ├── client.py                 LedgerClient: LocalClient (in process) and HttpClient (httpx)
│   ├── submit.py                 decide() and submit(): validate → identity → gate set → gates → enforce → write
│   ├── gates.py                  Gate seam: GateKind, Gate, GatePlugin, ConfigAuthorizer, BindingValidator, ReasonerScope, EvidenceVerifier, discovery, check_gate_set, invoke_gate, verify_evidence
│   ├── identity/                 The identity step: resolver plugin seam and discovery, the passthrough
│   ├── enforcement/              Gate-level tiers, decision-level rules, the combinator
│   ├── audit/                    Bundle assembly, BundleStore and replay, registration snapshots and API registrations, resolution and entity journals, the evidence escrow (ArtifactStore), sql/ (canonical DDL)
│   ├── plugin.py                 ReasonerPlugin protocol, FrameworkServices
│   ├── plugins.py                Reasoner plugin discovery, the registration file, build_registry
│   ├── reasoner_registry.py      LiveReasonerRegistry: plugin, file, and API registrations (DR-33)
│   ├── bootstrap.py              build_framework_services: the one place the shared services are built
│   ├── schemas.py                Exports schemas/submission-v2.json and bundle-v2.json (`make schemas`)
│   ├── monitoring/               Structured logging, duration_ms
│   └── settings.py               FrameworkSettings (no prefix: POSTGRES_*, LOG_*, DECISIONLEDGER_*)
│
├── eval/                     Framework eval harness (reasoner- and gate-agnostic)
│   ├── adapter.py                EvalAdapter / EvalDriver protocols, entry-point loader
│   ├── dimensions/               consistency, robustness, shadow (plus the shared base and the skipped-dimension result)
│   └── runners/                  harness.py: `--reasoner-id` selects the reasoner's adapter
│
├── gates/                    Gate plugins, one workspace member each (DR-28)
│   ├── rules/                    decisionledger-gate-rules (rules_gate, gate_id rules): declarative rule sets, no infrastructure
│   └── policy_rag/               decisionledger-gate-policy-rag (policy_rag_gate, gate_id policy): corpus references (corpus.py), retrieval providers (providers/), prompts, LLM adapters, its own sql/, es/, compose.yaml, eval/
│
├── reasoners/                Reasoner plugins, one workspace member each (DR-26)
│   ├── account_takeover/         ato-reasoner (ato_reasoner): the reference reasoner, its own service, Redis, XGBoost scorer
│   ├── example/                  example-reasoner (example_reasoner): a minimal documented reasoner (widgets)
│   ├── mock_stream/              mock-stream-reasoner (mock_stream_reasoner): the real-time event scorer pattern (DR-35)
│   ├── mock_batch/               mock-batch-reasoner (mock_batch_reasoner): the daily batch scorer pattern (DR-35)
│   ├── mock_text/                mock-text-reasoner (mock_text_reasoner): the LLM-native classifier pattern (DR-35)
│   └── mock_daily/               mock-daily-reasoner (mock_daily_reasoner): the deterministic computation with exceptions pattern (DR-35)
│
├── integrations/
│   └── mcp/                      decisionledger-mcp (decisionledger_mcp): the ledger as seven MCP tools over HttpClient
│
├── conformance/              Black-box HTTP pytest suite and example payloads, run against any instance (`make conformance`)
├── schemas/                  submission-v2.json, bundle-v2.json: exported from the models; tests/test_schemas.py fails on drift
├── tests/                    Framework tests; test-only doubles _stub_reasoner/, _fake_gate/, _stub_resolver/
├── outputs/                  Eval baselines (outputs/eval/, tracked); staged reports (outputs/stage/, gitignored)
│
└── docs/                     This site: design/, reference/ (API reference), operations/, development/
    └── assets/diagrams/          Rendered SVGs (committed; regenerate with `just diagrams`)
```

Each reasoner member owns its whole domain (for ATO: events, ingestion,
features, model, assembler, service, settings, prompts, policy corpus, eval
adapter and datasets) and any infrastructure only it uses. Each gate member owns its infrastructure and
its eval. Their layouts are in their own READMEs and in
[Reasoners](reasoners/index.md) and [Gates](gates/index.md).

---

## Boundaries and Dependency Rules

```
reasoners/<member>    ──→ core/, app/            (its pyproject depends on decision-ledger)
gates/<member>        ──→ core/, app/            reasoner-agnostic
integrations/<member> ──→ core/, app.client, app.api.models
eval/                 ──→ core/, app/
app/                  ──→ core/, PostgreSQL
core/                 ──→ the standard library and Pydantic only

app/, eval/           ··→ plugins                entry points only, never an import:
                                                 decisionledger.reasoners           (app/plugins.py)
                                                 decisionledger.gates               (app/gates.py)
                                                 decisionledger.identity_resolvers  (app/identity/resolvers.py)
                                                 decisionledger.eval_adapters       (eval/adapter.py)
conformance/          ··→ a running ledger       over HTTP only
```

`tests/test_framework_boundary.py` and `tests/core/test_import_boundary.py`
make these rules build failures:

- **The framework names no plugin.** No module under `core/`, `app/`, or
  `eval/` imports a workspace member (reasoner, gate, or integration, read
  from the members' pyprojects) or anything under `tests/`, and no framework
  `.py`, `.yaml`, `.sql`, or `.json` file names a reasoner's symbols, ids,
  templates, features, or jurisdictions, a gate member's vocabulary, or the
  infrastructure a gate owns. The framework mounts no reasoner router and
  builds no reasoner services.
- **`core/` is pure.** It imports nothing from `app/` or a reasoner; in
  practice it imports only the standard library and Pydantic (DR-8). The test
  for `core/`: can this code be unit-tested without Docker running? If not,
  it does not belong there.
- **Inside `core/`** (DR-11): `core/gate/` does not import `core/observation/`
  (so `GateRequest`, which carries the submission into the gate layer, lives
  at `core/gate_request.py`), `core/observation/` does not import
  `core.gate.output`, and `core/identity.py` is a leaf that imports nothing
  from the rest of `core`.
- **Gates are reasoner-agnostic.** A gate member imports no reasoner and names
  no ATO token in its source, DDL, or index mapping, and its DDL never touches
  the framework's `decisionledger` schema. The framework never defines or
  imports a `GateOutput`, `GateInput`, or `GateVerdict` subclass.
- **Integrations are clients.** An integration member imports from `app` only
  `app.client` and `app.api.models`, imports no other member, and publishes no
  plugin.
- **The conformance suite is a black box.** It imports only the standard
  library, `pytest`, `httpx`, `yaml`, and its own helpers.
- **LLM SDKs are confined** (DR-23). `openai` and `anthropic` are imported only
  by the RAG policy gate's `llm/` adapters, in every tree; the CI `boundary`
  job repeats the check with a grep.
- **The framework's infrastructure is Postgres.** The framework DDL creates no
  extension.
- **Build metadata.** The root package depends on exactly the framework set
  (pydantic, fastapi, starlette, uvicorn, httpx, psycopg, pyyaml, orjson,
  pydantic-settings, structlog), declares no optional extras and no entry
  points, and its wheel ships only `core`, `app`, and `eval`. Each reasoner
  member declares its own `decisionledger.reasoners` and
  `decisionledger.eval_adapters` entry points, and each gate member its own
  `decisionledger.gates` entry point. The workspace members are exactly
  `reasoners/*`, `gates/*`, and `integrations/*`.

Which plugins a checkout installs is a deployment choice, recorded in the root
`reasoners`, `gates`, and `integrations` dependency groups, which `uv sync`
installs by default. These boundaries buy testability (framework tests run
against test doubles, with no Docker for unit tests), modularity (a new
reasoner or gate is a new member with no framework change; `tests/_stub_reasoner`
and `tests/_fake_gate` prove it), and legibility (`core/` reads as the whole
decision model without any reasoner's types).

---

## Component Overview

Framework components (the ledger):

| Component | Location | Role |
|-----------|----------|------|
| Ledger API | `app/main.py`, `app/api/` | Routes, operations, problem documents |
| Auth | `app/auth.py` | API keys to principals; tenant-scoped reads |
| Clients | `app/client.py` | `LocalClient` and `HttpClient`, one `LedgerClient` protocol |
| Intake and orchestration | `app/submit.py`, `core/submission.py` | Validation, gate set, mandatory then advisory gates |
| Identity step | `app/identity/`, `core/identity.py` | Resolver plugin or passthrough; the subject on every bundle |
| Gate seam | `app/gates.py` | Discovery, build, binding admission, fail-closed invocation, fail-safe verify |
| Deterministic enforcement | `app/enforcement/`, `core/gate/combinator.py` | Gate-level tiers, decision-level rules, most restrictive action |
| Bundle store and replay | `app/audit/` | Version-2 bundles, replay, registration snapshots, journals, evidence escrow |
| Contract artifacts | `app/schemas.py`, `schemas/`, `conformance/` | JSON schemas and the black-box HTTP suite |
| Eval harness | `eval/` | Consistency, robustness, and shadow dimensions per reasoner |
| Monitoring | `app/monitoring/` | Structured JSON logging, `duration_ms` |

Members (plugins and clients the framework never imports):

| Component | Location | Role |
|-----------|----------|------|
| Rules gate | `gates/rules/` | Declarative rule sets over a reasoner's evidence |
| RAG policy gate | `gates/policy_rag/` | Retrieval through providers from the corpora a binding names, prompt rendering, LLM verdict; its own eval |
| Account Takeover | `reasoners/account_takeover/` | Reference reasoner: ingestion, features, scorer, assembler, its own service |
| Example reasoner | `reasoners/example/` | Minimal documented reasoner for the conformance suite and new teams |
| Reference-pattern mocks | `reasoners/mock_{stream,batch,text,daily}/` | One mock reasoner per reasoner pattern (DR-35): the real-time event scorer, the daily batch scorer, the LLM-native classifier, and the deterministic computation with exceptions |
| MCP server | `integrations/mcp/` | Seven tools for agent reasoners over `HttpClient` |

---

## Naming Conventions

| Context | Form | Example |
|---------|------|---------|
| Product and prose | one word | "DecisionLedger" |
| Repository, root distribution | kebab-case | `decision-ledger` |
| Framework import packages | snake_case | `core`, `app`, `eval` |
| Member distribution | kebab-case | `ato-reasoner`, `decisionledger-gate-rules`, `decisionledger-mcp` |
| Member import package | snake_case | `ato_reasoner`, `policy_rag_gate`, `decisionledger_mcp` |
| Reasoner id (entry-point name) | kebab-case | `ato-reasoner`, `example-reasoner` |
| Gate id (entry-point name) | lowercase | `policy`, `rules` |
| Entry-point groups | dotted | `decisionledger.gates` |
| Environment variables | UPPER_SNAKE_CASE; a member's settings carry its prefix | `DECISIONLEDGER_API_KEYS` (framework), `ATO_LEDGER_URL`, `POLICY_RAG_GATE_ELASTICSEARCH_URL`, `RULES_GATE_RULESETS_DIR` |
| Classes and Pydantic models | PascalCase | `DecisionBundle`, `GateOutput` |
| Enum members | UPPER_SNAKE_CASE | `DecisionAction.HOLD` |
| Problem codes | UPPER_SNAKE_CASE | `BYPASS_RULE_UNREGISTERED` |
| Feature names | snake_case strings | `"velocity_1min"` (ATO) |
| Make targets of a member | suffixed by member | `serve-account_takeover`, `eval-gate-policy_rag` |
