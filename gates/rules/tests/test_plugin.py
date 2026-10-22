"""Behavioral tests for the rules gate plugin — what the framework discovers.

Pins the published identity (``gate_id="rules"``, kind ``rules``,
deterministic, ``RulesGateConfig`` as the config model), the installed
``decisionledger.gates`` entry point, and ``build``: rule sets load from
``RULES_GATE_RULESETS_DIR``, a blank setting registers none, and a missing
directory fails the build loudly.
"""

from __future__ import annotations

import importlib.metadata as metadata

import pytest

from app.gates import GATE_GROUP, GateKind, check_gate_plugin
from rules_gate import PLUGIN, RulesGate, RulesGateConfig, RulesGatePlugin
from rules_gate.settings import RulesGateSettings


def test_the_plugin_satisfies_the_gate_plugin_protocol():
    assert check_gate_plugin(PLUGIN, name="rules") is PLUGIN
    assert PLUGIN.gate_id == "rules"
    assert PLUGIN.kind == GateKind.RULES
    assert PLUGIN.deterministic is True
    assert PLUGIN.config_model is RulesGateConfig


def test_the_installed_entry_point_resolves_to_the_plugin():
    (entry,) = [
        ep for ep in metadata.entry_points(group=GATE_GROUP) if ep.name == "rules"
    ]
    assert entry.value == "rules_gate.plugin:PLUGIN"
    assert entry.load() is PLUGIN


def test_build_loads_the_rulesets_from_the_configured_directory(write_ruleset):
    directory = write_ruleset()
    plugin = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(directory)))

    gate = plugin.build(framework=None)

    assert isinstance(gate, RulesGate)
    assert gate.ruleset_ids == ["widget-guardrails"]


def test_build_with_no_directory_registers_no_rulesets():
    plugin = RulesGatePlugin(RulesGateSettings(rulesets_dir=""))

    assert plugin.build(framework=None).ruleset_ids == []


def test_build_fails_fast_on_a_missing_directory(tmp_path):
    plugin = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(tmp_path / "absent")))

    with pytest.raises(FileNotFoundError):
        plugin.build(framework=None)


def test_settings_read_the_prefixed_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("RULES_GATE_RULESETS_DIR", str(tmp_path))

    assert RulesGatePlugin().settings.rulesets_dir == str(tmp_path)
