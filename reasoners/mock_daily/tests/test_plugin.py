"""The daily mock's plugin: what a deployment that installs it discovers.

``PLUGIN`` publishes the registration and two empty asset directories (the
rules gate reads no prompt and no corpus); it is installed under both
entry-point groups by its reasoner id, and its CLI as a console script. Its
registration binds the rules gate mandatory, with no configuration (every
submission names its rule set), records ALLOW only, under its one bypass
rule (``within-tolerance``: an absolute drift below ``TOLERANCE_PCT``),
names its subjects, and declares no novel-entity feature. Its
``reasoner.mk`` stages the member's rule set and joins the mock evals,
never the reasoner targets.
"""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import mock_daily_reasoner
from app.plugin import ReasonerPlugin
from app.plugins import PLUGIN_GROUP, discover_plugins
from core.actions import DecisionAction
from core.gate import Compare
from eval.adapter import EVAL_ADAPTER_GROUP, load_eval_adapter
from mock_daily_reasoner.__main__ import main
from mock_daily_reasoner.eval.adapter import ADAPTER
from mock_daily_reasoner.plugin import PLUGIN, RULESETS_DIR
from mock_daily_reasoner.registry import (
    MOCK_DAILY_ACCOUNT_NAMESPACE,
    MOCK_DAILY_REGISTRATION,
    RULES_GATE_BINDING,
    TOLERANCE_PCT,
    WITHIN_TOLERANCE_RULE,
)
from mock_daily_reasoner.settlements import AccountSettlement

REASONER_ID = "mock-daily-reasoner"
#: The package directory (``src/mock_daily_reasoner``).
PACKAGE_DIR = Path(mock_daily_reasoner.__file__).resolve().parent


def _entry_point(group):
    [match] = [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if ep.name == REASONER_ID
    ]
    return match


def test_the_plugin_publishes_the_registration_and_empty_asset_directories():
    assert isinstance(PLUGIN, ReasonerPlugin)
    assert PLUGIN.registration is MOCK_DAILY_REGISTRATION
    assert PLUGIN.registration.reasoner_id == REASONER_ID
    assert (PLUGIN.prompts_dir, PLUGIN.corpus_dir) == (
        PACKAGE_DIR / "prompts",
        PACKAGE_DIR / "corpus",
    )
    for directory in (PLUGIN.prompts_dir, PLUGIN.corpus_dir):
        assert directory.is_dir()
        assert (directory / ".gitkeep").is_file()
        assert [p.name for p in directory.iterdir() if not p.name.startswith(".")] == []
        assert list(directory.rglob("*.yaml")) == []
        assert list(directory.rglob("*.md")) == []


def test_the_installed_entry_points_resolve_to_the_singletons():
    assert _entry_point(PLUGIN_GROUP).load() is PLUGIN
    assert _entry_point(EVAL_ADAPTER_GROUP).load() is ADAPTER
    assert load_eval_adapter(REASONER_ID) is ADAPTER
    assert (ADAPTER.reasoner_id, ADAPTER.event_type) == (
        REASONER_ID,
        AccountSettlement,
    )
    assert _entry_point("console_scripts").load() is main


def test_discovery_finds_the_mock_by_its_reasoner_id():
    [plugin] = discover_plugins(allow=frozenset({REASONER_ID}))

    assert plugin is PLUGIN


def test_the_registration_binds_the_rules_gate_mandatory_with_an_empty_config():
    [binding] = MOCK_DAILY_REGISTRATION.gates

    assert (binding.gate_id, binding.mandatory, binding.config) == ("rules", True, {})
    assert binding == RULES_GATE_BINDING
    assert MOCK_DAILY_REGISTRATION.gate_ids == frozenset({"rules"})


def test_the_registration_records_only_allow_under_its_one_bypass_rule():
    registration = MOCK_DAILY_REGISTRATION

    [rule] = registration.bypass_rules
    assert rule is WITHIN_TOLERANCE_RULE
    assert (rule.rule_id, rule.version, rule.action) == (
        "within-tolerance",
        "1",
        DecisionAction.ALLOW,
    )
    assert rule.predicate == Compare(
        field="feature_set.abs_drift_pct", op="lt", value=TOLERANCE_PCT
    )
    assert TOLERANCE_PCT == 1.0
    assert registration.recordable_actions == frozenset({DecisionAction.ALLOW})


def test_the_registration_names_its_subjects_and_no_novel_entity_feature():
    registration = MOCK_DAILY_REGISTRATION

    assert registration.native_namespaces == (MOCK_DAILY_ACCOUNT_NAMESPACE,)
    namespace = registration.native_namespace("mock-daily-account")
    assert namespace is not None
    assert namespace.entity_type == "account"
    assert registration.novel_entity_feature is None
    assert registration.jurisdiction_conflict_groups == ()


def test_the_make_include_stages_the_rule_set_and_joins_only_the_mock_evals(
    member_dir,
):
    makefile = (member_dir / "reasoner.mk").read_text(encoding="utf-8")
    appended = [
        (name, value)
        for name, value in re.findall(
            r"^([A-Z_]+)\s*\+=\s*(.*?)\s*$", makefile, re.MULTILINE
        )
        if name != "REASONER_HELP"
    ]
    assigned = set(re.findall(r"^([A-Z_]+)\s*[+:?]?=", makefile, re.MULTILINE))
    member = re.search(r"^MOCK_DAILY_DIR\s*:=\s*(\S+)\s*$", makefile, re.MULTILINE)
    reasoner = re.search(
        r"^MOCK_DAILY_REASONER_ID\s*:=\s*(\S+)\s*$", makefile, re.MULTILINE
    )

    assert appended == [
        ("RULESET_DIRS", "$(MOCK_DAILY_DIR)/rulesets"),
        ("MOCK_EVAL_TARGETS", "eval-mock_daily"),
    ]
    assert not assigned & {
        "REASONER_EVAL_TARGETS",
        "REASONER_PREPARE_TARGETS",
        "REASONER_INDEX_TARGETS",
    }
    assert member is not None
    assert member_dir.parents[1] / member.group(1) == member_dir
    assert member_dir / "rulesets" == RULESETS_DIR
    assert reasoner is not None
    assert reasoner.group(1) == REASONER_ID
    assert re.search(
        r"^eval-mock_daily:\n\t.*eval\.runners\.harness "
        r"--reasoner-id \$\(MOCK_DAILY_REASONER_ID\)$",
        makefile,
        re.MULTILINE,
    )
