"""DecisionLedger framework runtime — imports from core/, adds infrastructure.

This package wires core/ contracts to the framework's own infrastructure
(PostgreSQL: the audit ledger, its only infrastructure) and composes
plugins. It knows no reasoner and no gate by name: reasoners are discovered
as ``ReasonerPlugin`` objects through the ``decisionledger.reasoners`` entry
point group (``app/plugins.py``), gates as ``GatePlugin`` objects through
the ``decisionledger.gates`` group (``app/gates.py``), and both are composed
by ``app/main.py:create_app``. Every gate — the reference ones included —
is a workspace member under ``gates/`` that owns its own infrastructure
(DR-28). Nothing in the wider codebase imports from app/ except app/
itself, reasoner packages, and gate packages.

Dependency rule: app/ may import from core/. core/ must never import from app/.
app/ must never import from a reasoner package or a gate member.

Modules and subpackages:
    plugin:       ``ReasonerPlugin`` / ``FrameworkServices`` seam.
    plugins:      Reasoner entry-point discovery, allowlist, registry assembly.
    gates:        ``GatePlugin`` / ``Gate`` / ``ConfigAuthorizer`` seam, gate
                  discovery and building.
    bootstrap:    Builds the shared services and the composed gates once per
                  process.
    decide:       ``execute_decision`` — the framework half of every decision.
    enforcement:  Deterministic rule-based action resolution.
    audit:        Decision Bundle construction, persistence, and replay.
    monitoring:   Structured JSON logging and observability.
"""
