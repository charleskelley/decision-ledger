"""The member's rule set: what the rules gate decides every decision under.

``rulesets/mock-daily-v1.yaml`` is deployment configuration the rules gate
reads from its rule-set directory: it names the plugin's reasoner id (the
gate fails closed for any other reasoner), and it is the only rule set in
the member's directory. Its rules read only ``abs_drift_pct``, which every
row carries, so no rule ever fails on a missing key. It and the bypass rule
are complementary: the bypass rule's tolerance is the rule set's mildest
threshold and its default is the record's action, so a row the bypass rule
records matches no rule and keeps its ALLOW, and every exception matches at
least one rule, at each threshold exactly and either side of it, short or
over. Through the ledger, the gate decides each band of drift as the rule
set documents: within 1% allowed by default, from 1% challenged, from 5%
held, and from 20% blocked for investigation, shortfalls and overages alike.
"""

from __future__ import annotations

import pytest
import yaml

from core.actions import DecisionAction
from core.gate import evaluate_predicate, referenced_fields
from core.submission import matching_bypass_rule
from mock_daily_reasoner.assembler import build_submission, reasoner_context
from mock_daily_reasoner.compute import compute_drift
from mock_daily_reasoner.plugin import PLUGIN, RULESETS_DIR
from mock_daily_reasoner.registry import (
    MOCK_DAILY_REGISTRATION,
    RULESET_ID,
    TOLERANCE_PCT,
    WITHIN_TOLERANCE_RULE,
)
from mock_daily_reasoner.synthetic import synthetic_day
from rules_gate.ruleset import load_rulesets, read_ruleset

#: The shipped rule-set file.
RULESET_FILE = RULESETS_DIR / "mock-daily-v1.yaml"
#: Sizes of drift in percent: none, each threshold exactly and either side of
#: it, and well past the last; with a target of 10,000 every one is exact.
GRID = (
    0.0,
    0.01,
    0.5,
    0.99,
    1.0,
    1.01,
    2.5,
    4.99,
    5.0,
    5.01,
    12.5,
    19.99,
    20.0,
    20.01,
    60.0,
    100.0,
)
ALLOW, CHALLENGE, HOLD, BLOCK = (
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
)
SEVERE = ["drift-severe", "drift-major", "drift-minor"]


def test_the_shipped_rule_set_names_the_plugins_reasoner_id():
    ruleset = read_ruleset(RULESET_FILE)

    assert ruleset.reasoner_id == PLUGIN.registration.reasoner_id
    assert ruleset.ruleset_id == RULESET_ID == "mock-daily-v1"
    assert ruleset.ref == "mock-daily-v1@1"


def test_the_members_rule_set_directory_holds_exactly_that_rule_set():
    loaded = load_rulesets(RULESETS_DIR)

    assert list(loaded) == [RULESET_ID]
    assert loaded[RULESET_ID].origin == RULESET_FILE.name
    assert loaded[RULESET_ID].ruleset == read_ruleset(RULESET_FILE)


def test_every_rule_reads_only_abs_drift_pct_which_every_row_carries(business_date):
    ruleset = read_ruleset(RULESET_FILE)
    read = set().union(*(referenced_fields(rule.when) for rule in ruleset.rules))
    # A synthetic day draws every band, exceptions of every severity included.
    rows = synthetic_day(200, business_date=business_date, run_tag="rules")

    assert read == {"feature_set.abs_drift_pct"}
    assert referenced_fields(WITHIN_TOLERANCE_RULE.predicate) == read
    assert all("abs_drift_pct" in compute_drift(row).feature_set for row in rows)


def test_the_bypass_tolerance_is_the_rule_sets_mildest_threshold():
    raw = yaml.safe_load(RULESET_FILE.read_text(encoding="utf-8"))
    whens = [rule["when"] for rule in raw["rules"]]
    predicate = WITHIN_TOLERANCE_RULE.predicate

    assert {(w["kind"], w["field"], w["op"]) for w in whens} == {
        ("compare", "feature_set.abs_drift_pct", "ge")
    }
    assert min(w["value"] for w in whens) == TOLERANCE_PCT == 1.0
    assert (predicate.kind, predicate.field, predicate.op, predicate.value) == (
        "compare",
        "feature_set.abs_drift_pct",
        "lt",
        TOLERANCE_PCT,
    )
    # The default the gate permits when no rule matches is the record's action.
    assert raw["default_action"] == WITHIN_TOLERANCE_RULE.action.value == "ALLOW"


@pytest.mark.parametrize("sign", [-1.0, 1.0], ids=["shortfall", "overage"])
def test_the_bypass_rule_and_the_rule_set_are_complementary(settlement, sign):
    ruleset = read_ruleset(RULESET_FILE)
    target = 10_000.0

    for size in GRID:
        row = settlement(1, target=target, actual=round(target + sign * size * 100, 2))
        drift = compute_drift(row)
        assert drift.abs_drift_pct == size
        context = reasoner_context(drift)

        recorded = matching_bypass_rule(MOCK_DAILY_REGISTRATION, context) is not None
        matched = [
            rule.rule_id
            for rule in ruleset.rules
            if evaluate_predicate(rule.when, context)
        ]

        assert recorded == (size < TOLERANCE_PCT), size
        if recorded:
            assert matched == [], size  # the default: the record keeps ALLOW
        else:
            assert matched, size  # an exception is always decided by a rule
        # Each rule fires from its own threshold up.
        assert matched == [
            rule.rule_id for rule in ruleset.rules if size >= rule.when.value
        ], size


@pytest.mark.parametrize(
    ("amounts", "action", "matched"),
    [
        pytest.param((1_500.0, 1_500.0), ALLOW, [], id="settled-exactly"),
        pytest.param((2_500.0, 2_490.0), ALLOW, [], id="0.4pct-short"),
        pytest.param((800.0, 803.2), ALLOW, [], id="0.4pct-over"),
        pytest.param((10_000.0, 9_900.0), CHALLENGE, ["drift-minor"], id="1pct-short"),
        pytest.param((5_000.0, 5_150.0), CHALLENGE, ["drift-minor"], id="3pct-over"),
        pytest.param(
            (10_000.0, 9_200.0), HOLD, ["drift-major", "drift-minor"], id="8pct-short"
        ),
        pytest.param(
            (40_000.0, 46_000.0), HOLD, ["drift-major", "drift-minor"], id="15pct-over"
        ),
        pytest.param((10_000.0, 12_000.0), BLOCK, SEVERE, id="20pct-over"),
        pytest.param((12_000.0, 9_000.0), BLOCK, SEVERE, id="25pct-short"),
        pytest.param((1_000.0, 1_600.0), BLOCK, SEVERE, id="60pct-over"),
    ],
)
async def test_the_rules_gate_decides_each_band_of_drift_as_documented(
    client, settlement, amounts, action, matched
):
    target, actual = amounts
    row = settlement(1, target=target, actual=actual)
    drift = compute_drift(row)
    submission = build_submission(row, drift)
    recorded = drift.abs_drift_pct < TOLERANCE_PCT
    assert submission.mode == ("record" if recorded else "gate")

    receipt = await client.submit(submission)
    bundle = await client.get_bundle(receipt.decision_id)

    assert receipt.decision_action == action
    assert receipt.decision_level_rule is None
    assert receipt.bypass_rule_holds is (True if recorded else None)
    record = bundle.gate("rules")
    assert record is not None
    assert (record.status, record.mandatory) == ("ran", True)
    assert record.enforced_action == action
    output = record.output
    assert output is not None
    assert "failure" not in output.extra
    assert output.verdict is not None
    snapshot = output.evidence_snapshot
    assert isinstance(snapshot, dict)
    assert snapshot["ruleset_id"] == RULESET_ID
    assert snapshot["matched_rule_ids"] == matched
    assert snapshot["default_applied"] == (not matched)
    assert snapshot["observed"] == {"feature_set.abs_drift_pct": drift.abs_drift_pct}
    assert [ref.locator for ref in output.evidence] == (matched or ["default_action"])
    assert {ref.source_id for ref in output.evidence} == {"mock-daily-v1@1"}
    expected_controls = ["settlement_investigation"] if action == BLOCK else []
    assert output.verdict.required_controls == expected_controls
