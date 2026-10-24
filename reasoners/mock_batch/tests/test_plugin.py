"""The batch mock's plugin: what a deployment that installs it discovers.

``PLUGIN`` publishes the registration and two empty asset directories (the
rules gate reads no prompt and no corpus); it is installed under both
entry-point groups by its reasoner id. Its registration binds the rules gate
(advisory, with no configuration: every submission names its rule set),
records one band of ``delinquency_score`` per action, all four recordable,
with disjoint bypass rules that leave a deliberate gap (a flagged row
matches none, so it is an exception for the gate), names its subjects, and
declares no novel-entity feature. Its ``reasoner.mk`` stages the member's
rule sets and joins the mock evals, never the reasoner targets.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import re
from pathlib import Path

import pytest

import mock_batch_reasoner
from app.plugin import ReasonerPlugin
from app.plugins import PLUGIN_GROUP, discover_plugins
from core.actions import DecisionAction
from core.submission import evaluate_bypass, matching_bypass_rule
from eval.adapter import EVAL_ADAPTER_GROUP, load_eval_adapter
from mock_batch_reasoner.assembler import reasoner_context
from mock_batch_reasoner.eval.adapter import ADAPTER
from mock_batch_reasoner.plugin import PLUGIN, RULESETS_DIR
from mock_batch_reasoner.registry import (
    CHARGE_OFF_THRESHOLD,
    HOLD_THRESHOLD,
    MOCK_BATCH_ACCOUNT_NAMESPACE,
    MOCK_BATCH_REGISTRATION,
    RULES_GATE_BINDING,
    WATCH_THRESHOLD,
)
from mock_batch_reasoner.rows import AccountDay
from mock_batch_reasoner.scorer import score_row

REASONER_ID = "mock-batch-reasoner"
#: The package directory (``src/mock_batch_reasoner``).
PACKAGE_DIR = Path(mock_batch_reasoner.__file__).resolve().parent
#: Each band's upper bound (exclusive) and the rule an unflagged account
#: scored in it is recorded under.
BANDS = (
    (WATCH_THRESHOLD, "current-account"),
    (HOLD_THRESHOLD, "watch-list"),
    (CHARGE_OFF_THRESHOLD, "delinquent-hold"),
    (float("inf"), "charge-off"),
)
#: Scores over [0, 1] in steps of 0.01, with each threshold exactly.
GRID = sorted(
    {round(step / 100, 2) for step in range(101)}
    | {WATCH_THRESHOLD, HOLD_THRESHOLD, CHARGE_OFF_THRESHOLD}
)


def _entry_point(group):
    [match] = [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if ep.name == REASONER_ID
    ]
    return match


def _band(score):
    """The rule an unflagged account scored ``score`` is recorded under."""
    return next(rule_id for upper, rule_id in BANDS if score < upper)


def test_the_plugin_publishes_the_registration_and_empty_asset_directories():
    assert isinstance(PLUGIN, ReasonerPlugin)
    assert PLUGIN.registration is MOCK_BATCH_REGISTRATION
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
    assert (ADAPTER.reasoner_id, ADAPTER.event_type) == (REASONER_ID, AccountDay)


def test_discovery_finds_the_mock_by_its_reasoner_id():
    [plugin] = discover_plugins(allow=frozenset({REASONER_ID}))

    assert plugin is PLUGIN


def test_the_registration_binds_the_rules_gate_advisory_with_an_empty_config():
    [binding] = MOCK_BATCH_REGISTRATION.gates

    assert (binding.gate_id, binding.mandatory, binding.config) == ("rules", False, {})
    assert binding == RULES_GATE_BINDING
    assert MOCK_BATCH_REGISTRATION.gate_ids == frozenset({"rules"})


def test_the_registration_records_one_rule_per_action_all_four_recordable():
    registration = MOCK_BATCH_REGISTRATION

    assert [rule.rule_id for rule in registration.bypass_rules] == [
        "current-account",
        "watch-list",
        "delinquent-hold",
        "charge-off",
    ]
    assert [rule.action for rule in registration.bypass_rules] == [
        DecisionAction.ALLOW,
        DecisionAction.CHALLENGE,
        DecisionAction.HOLD,
        DecisionAction.BLOCK,
    ]
    assert registration.recordable_actions == frozenset(DecisionAction)
    assert {rule.version for rule in registration.bypass_rules} == {"1"}
    assert WATCH_THRESHOLD < HOLD_THRESHOLD < CHARGE_OFF_THRESHOLD


@pytest.mark.parametrize(
    "flagged", [pytest.param(False, id="unflagged"), pytest.param(True, id="flagged")]
)
def test_the_bypass_rules_are_disjoint_and_leave_a_flagged_row_to_the_gate(
    row, flagged
):
    scored = score_row(row(1, needs_review=flagged))

    for score in GRID:
        context = reasoner_context(dataclasses.replace(scored, delinquency_score=score))
        checks = [
            evaluate_bypass(rule, context)
            for rule in MOCK_BATCH_REGISTRATION.bypass_rules
        ]
        holding = [check.rule.rule_id for check in checks if check.holds]
        matched = matching_bypass_rule(MOCK_BATCH_REGISTRATION, context)

        assert [check.error for check in checks] == [None] * len(checks), score
        if flagged:
            assert holding == [], score
            assert matched is None, score
        else:
            assert holding == [_band(score)], score
            assert matched is not None
            assert matched.rule_id == _band(score), score


def test_each_threshold_opens_the_next_band(row):
    scored = score_row(row(1))

    def band_at(score):
        context = reasoner_context(dataclasses.replace(scored, delinquency_score=score))
        rule = matching_bypass_rule(MOCK_BATCH_REGISTRATION, context)
        assert rule is not None
        return rule.rule_id

    assert [
        band_at(score)
        for score in (0.0, WATCH_THRESHOLD, HOLD_THRESHOLD, CHARGE_OFF_THRESHOLD, 1.0)
    ] == [
        "current-account",
        "watch-list",
        "delinquent-hold",
        "charge-off",
        "charge-off",
    ]


def test_the_registration_names_its_subjects_and_no_novel_entity_feature():
    registration = MOCK_BATCH_REGISTRATION

    assert registration.native_namespaces == (MOCK_BATCH_ACCOUNT_NAMESPACE,)
    namespace = registration.native_namespace("mock-batch-account")
    assert namespace is not None
    assert namespace.entity_type == "account"
    assert registration.novel_entity_feature is None


def test_the_make_include_stages_the_rule_sets_and_joins_only_the_mock_evals(
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
    member = re.search(r"^MOCK_BATCH_DIR\s*:=\s*(\S+)\s*$", makefile, re.MULTILINE)
    reasoner = re.search(
        r"^MOCK_BATCH_REASONER_ID\s*:=\s*(\S+)\s*$", makefile, re.MULTILINE
    )

    assert appended == [
        ("RULESET_DIRS", "$(MOCK_BATCH_DIR)/rulesets"),
        ("MOCK_EVAL_TARGETS", "eval-mock_batch"),
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
        r"^eval-mock_batch:\n\t.*eval\.runners\.harness "
        r"--reasoner-id \$\(MOCK_BATCH_REASONER_ID\)$",
        makefile,
        re.MULTILINE,
    )
