"""The example plugin satisfies the framework's contracts and is discoverable."""

from __future__ import annotations

import importlib.metadata

from app.plugin import ReasonerPlugin
from app.plugins import PLUGIN_GROUP, discover_plugins
from eval.adapter import EVAL_ADAPTER_GROUP, load_eval_adapter
from example_reasoner.eval.adapter import ADAPTER
from example_reasoner.plugin import PLUGIN
from example_reasoner.registry import EXAMPLE_REGISTRATION


def _entry_point(group: str) -> importlib.metadata.EntryPoint:
    [match] = [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if ep.name == EXAMPLE_REGISTRATION.reasoner_id
    ]
    return match


def test_the_plugin_publishes_its_registration_and_assets_only():
    assert isinstance(PLUGIN, ReasonerPlugin)
    assert PLUGIN.registration is EXAMPLE_REGISTRATION
    assert sorted(p.name for p in PLUGIN.prompts_dir.glob("*.yaml")) == [
        "example-v1.yaml"
    ]
    assert len(sorted(PLUGIN.corpus_dir.glob("*.md"))) == 3


def test_the_installed_entry_points_resolve_to_the_singletons():
    assert _entry_point(PLUGIN_GROUP).load() is PLUGIN
    assert _entry_point(EVAL_ADAPTER_GROUP).load() is ADAPTER
    assert load_eval_adapter(EXAMPLE_REGISTRATION.reasoner_id) is ADAPTER


def test_discovery_finds_the_example_by_its_reasoner_id():
    [plugin] = discover_plugins(allow=frozenset({"example-reasoner"}))
    assert plugin is PLUGIN


def test_the_registration_records_or_gates_through_the_policy_gate():
    registration = EXAMPLE_REGISTRATION
    assert [(b.gate_id, b.mandatory) for b in registration.gates] == [("policy", False)]
    assert [r.rule_id for r in registration.bypass_rules] == [
        "low-score",
        "high-score",
    ]
    assert registration.novel_entity_feature == "thin_history"
