"""Behavioral tests for rule sets — the rules gate's registered configuration.

Rule sets come from YAML files the deployment mounts. These tests pin the
file format (the documented shape parses; enum and list values map onto the
strict models), the validation that keeps a malformed rule set from ever
loading, and the directory loader's fail-fast behavior.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import AllOf, Compare
from rules_gate import RuleSet, RulesGateConfig, load_rulesets, parse_ruleset


def test_the_documented_yaml_shape_parses_into_a_ruleset(ruleset_data):
    ruleset = parse_ruleset(ruleset_data)

    assert isinstance(ruleset, RuleSet)
    assert ruleset.ref == "widget-guardrails@1.0.0"
    assert ruleset.reasoner_id == "widget-reasoner"
    assert ruleset.default_action == DecisionAction.ALLOW
    assert [r.rule_id for r in ruleset.rules] == [
        "thin-history-hold",
        "extreme-score-block",
        "wobbly-region-challenge",
    ]
    assert ruleset.rules[0].action == DecisionAction.HOLD
    assert ruleset.rules[0].required_controls == ("calibration_review",)
    assert isinstance(ruleset.rules[1].when, Compare)
    assert isinstance(ruleset.rules[2].when, AllOf)


def test_a_ruleset_round_trips_through_its_json_dump(ruleset_data):
    ruleset = parse_ruleset(ruleset_data)
    assert parse_ruleset(ruleset.model_dump(mode="json")) == ruleset


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda d: d["rules"].append(dict(d["rules"][0])), "duplicate rule_id"),
        (lambda d: d.update(rules=[]), "rules"),
        (lambda d: d.update(default_action="MAYBE"), "default_action"),
        (lambda d: d["rules"][0].update(action="ESCALATE"), "action"),
        (lambda d: d["rules"][0].update(when={"kind": "xor"}), "when"),
        (lambda d: d.update(owner="someone"), "owner"),
        (lambda d: d.pop("reasoner_id"), "reasoner_id"),
        (lambda d: d.update(version=""), "version"),
    ],
    ids=[
        "duplicate-rule-id",
        "no-rules",
        "unknown-default",
        "unknown-action",
        "bad-predicate",
        "unknown-key",
        "no-tenant",
        "empty-version",
    ],
)
def test_a_malformed_ruleset_is_rejected(ruleset_data, mutate, fragment):
    mutate(ruleset_data)
    with pytest.raises(ValidationError, match=fragment):
        parse_ruleset(ruleset_data)


def test_load_rulesets_reads_every_yaml_file_in_the_directory(
    write_ruleset, ruleset_data
):
    other = dict(ruleset_data, ruleset_id="second-set")
    directory = write_ruleset()
    write_ruleset(other, name="second.yml")

    rulesets = load_rulesets(directory)

    assert set(rulesets) == {"widget-guardrails", "second-set"}


def test_load_rulesets_rejects_two_files_with_one_ruleset_id(write_ruleset):
    write_ruleset(name="a.yaml")
    directory = write_ruleset(name="b.yaml")

    with pytest.raises(ValueError, match="Duplicate ruleset_id 'widget-guardrails'"):
        load_rulesets(directory)


def test_load_rulesets_rejects_a_file_that_is_not_a_mapping(tmp_path):
    (tmp_path / "list.yaml").write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(ValueError, match="YAML mapping"):
        load_rulesets(tmp_path)


def test_load_rulesets_fails_fast_on_a_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_rulesets(tmp_path / "absent")


def test_load_rulesets_of_an_empty_directory_registers_nothing(tmp_path):
    assert load_rulesets(tmp_path) == {}


def test_the_config_model_accepts_only_a_ruleset_id():
    assert RulesGateConfig.model_validate({"ruleset_id": "x"}).ruleset_id == "x"
    with pytest.raises(ValidationError):
        RulesGateConfig.model_validate({})
    with pytest.raises(ValidationError):
        RulesGateConfig.model_validate({"ruleset_id": "x", "rules": []})
    with pytest.raises(ValidationError):
        RulesGateConfig.model_validate({"ruleset_id": ""})
