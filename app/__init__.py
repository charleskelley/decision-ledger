"""DecisionLedger framework runtime — imports from core/, adds infrastructure.

This package wires core/ contracts to the framework's own infrastructure
(PostgreSQL: the audit ledger, its only infrastructure), composes plugins,
and serves the ledger's HTTP surface. It knows no reasoner and no gate by
name: reasoners are registered from ``ReasonerPlugin`` objects discovered
through the ``decisionledger.reasoners`` entry point group and from the
deployment's registration file (``app/plugins.py``), gates are discovered as
``GatePlugin`` objects through the ``decisionledger.gates`` group
(``app/gates.py``), and both are composed by ``app/main.py:create_app``.
Every gate — the reference ones included — is a workspace member under
``gates/`` that owns its own infrastructure (DR-28). The ledger runs no
reasoner code: a reasoner submits over HTTP or in process through
``app.client`` (DR-31). Nothing in the wider codebase imports from app/
except app/ itself, reasoner packages, gate packages, and integrations.

Dependency rule: app/ may import from core/. core/ must never import from app/.
app/ must never import from a reasoner package or a gate member.

Modules and subpackages:
    plugin:       ``ReasonerPlugin`` / ``FrameworkServices`` seam.
    plugins:      Reasoner entry-point discovery, allowlist, the registration
                  file, and registry assembly.
    gates:        ``GatePlugin`` / ``Gate`` / ``ConfigAuthorizer`` seam, gate
                  discovery and building.
    bootstrap:    Builds the shared services and the composed gates once per
                  process.
    submit:       ``decide`` / ``submit`` — submission intake, the gate set,
                  and the bundle write: the framework half of every decision.
    api:          The HTTP surface: decisions, batch, entities,
                  registrations, gates; problem-style errors.
    auth:         API keys to principals (reasoner, operator, trusted).
    client:       ``LocalClient`` (in process) and ``HttpClient`` — how a
                  reasoner submits.
    schemas:      Exports the wire contract's JSON schemas (``schemas/``).
    enforcement:  Deterministic rule-based action resolution.
    audit:        Decision Bundle construction, persistence, replay,
                  registration snapshots, and the resolution journal.
    monitoring:   Structured JSON logging and observability.
"""
