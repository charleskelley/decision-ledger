"""The ATO plugin satisfies the framework's ``ReasonerPlugin`` contract.

These checks are what makes the package discoverable: the installed entry
points resolve to the module-level ``PLUGIN`` / ``ADAPTER`` singletons, the
directories the framework reads (prompts, corpus) ship inside the package,
and every prompt template the registration allows is actually loadable.
"""

from __future__ import annotations

import importlib.metadata

from app.gate.policy.prompt_registry import YamlPromptRegistry
from app.plugin import ReasonerPlugin
from app.plugins import PLUGIN_GROUP, discover_plugins
from ato_reasoner.eval.adapter import ADAPTER
from ato_reasoner.plugin import PLUGIN
from ato_reasoner.registry import ATO_REGISTRATION
from eval.adapter import EVAL_ADAPTER_GROUP


def _entry_point(group: str) -> importlib.metadata.EntryPoint:
    matches = [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if ep.name == ATO_REGISTRATION.reasoner_id
    ]
    assert len(matches) == 1, matches
    return matches[0]


def test_ato_plugin_satisfies_the_reasoner_plugin_protocol():
    assert isinstance(PLUGIN, ReasonerPlugin)


def test_ato_plugin_carries_the_ato_registration():
    assert PLUGIN.registration is ATO_REGISTRATION


def test_ato_plugin_ships_its_prompts_and_corpus_inside_the_package():
    assert PLUGIN.prompts_dir.is_dir()
    assert PLUGIN.corpus_dir.is_dir()
    assert any(PLUGIN.prompts_dir.glob("*.yaml"))
    assert any(PLUGIN.corpus_dir.rglob("*.md"))


def test_every_allowed_prompt_template_is_loadable_from_the_plugin_prompts_dir():
    registered = set(YamlPromptRegistry([PLUGIN.prompts_dir]).registered_ids())
    allowed = set(ATO_REGISTRATION.allowed_prompt_template_ids)
    assert allowed <= registered, sorted(allowed - registered)


def test_installed_runtime_entry_point_resolves_to_the_plugin_singleton():
    assert _entry_point(PLUGIN_GROUP).load() is PLUGIN


def test_installed_eval_entry_point_resolves_to_the_adapter_singleton():
    assert _entry_point(EVAL_ADAPTER_GROUP).load() is ADAPTER


def test_framework_discovery_finds_the_ato_plugin_by_its_reasoner_id():
    plugins = discover_plugins(allow=frozenset({ATO_REGISTRATION.reasoner_id}))
    assert [p.registration.reasoner_id for p in plugins] == [
        ATO_REGISTRATION.reasoner_id
    ]
    assert plugins[0] is PLUGIN
