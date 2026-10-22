"""Rules gate — declarative predicates over a reasoner's evidence (DR-28).

A workspace member that plugs into DecisionLedger through the
``decisionledger.gates`` entry point ``rules``. It is the framework's
zero-infrastructure default gate: deployment-registered rule sets
(``RULES_GATE_RULESETS_DIR``) of predicates over ``label_value`` and
``feature_set`` (the framework's closed predicate language,
``core.gate.predicate``), evaluated deterministically. The framework never
imports this package.

Public API:
    RulesGate:          The runtime gate.
    RulesGatePlugin:    The plugin class; ``PLUGIN`` is the entry-point target.
    Rule, RuleSet:      A rule and a versioned, tenant-owned rule set.
    RulesGateConfig:    The gate's ``config_model`` (``ruleset_id``).
    RulesGateSnapshot:  Schema of the gate's ``evidence_snapshot``.
    load_rulesets:      Load every rule-set YAML file in a directory, each
                        with the file it came from (``LoadedRuleSet``).
    canonical_ruleset:  The canonical JSON the gate escrows and hashes.
"""

from rules_gate.gate import RULES_GATE_ID, RulesGate, RulesGateSnapshot
from rules_gate.plugin import PLUGIN, RulesGatePlugin
from rules_gate.ruleset import (
    LoadedRuleSet,
    Rule,
    RuleSet,
    RulesGateConfig,
    canonical_ruleset,
    load_rulesets,
    parse_ruleset,
    read_ruleset,
)
from rules_gate.settings import RulesGateSettings

__all__ = [
    "PLUGIN",
    "RULES_GATE_ID",
    "LoadedRuleSet",
    "Rule",
    "RuleSet",
    "RulesGate",
    "RulesGateConfig",
    "RulesGatePlugin",
    "RulesGateSettings",
    "RulesGateSnapshot",
    "canonical_ruleset",
    "load_rulesets",
    "parse_ruleset",
    "read_ruleset",
]
