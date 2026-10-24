"""The member's rule set: what the rules gate decides the mock's exceptions under.

``rulesets/mock-batch-v1.yaml`` is deployment configuration the rules gate
reads from its rule-set directory: it names the plugin's reasoner id (the
gate fails closed for any other reasoner), it is the only rule set in the
member's directory, and its rules read only features every row carries, so
no rule ever fails on a missing key. Through the ledger, the gate decides
each kind of flagged account as the rule set documents: a current one is
allowed by default, one 30 days past due challenged, one 60 days past due
or with a returned payment held, and one 90 days or more past due blocked
for collections.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.actions import DecisionAction
from core.gate import AllOf, AnyOf, Compare, Not
from mock_batch_reasoner.assembler import build_submission
from mock_batch_reasoner.plugin import PLUGIN, RULESETS_DIR
from mock_batch_reasoner.registry import RULESET_ID
from mock_batch_reasoner.scorer import score_row
from mock_batch_reasoner.synthetic import synthetic_day
from rules_gate.ruleset import load_rulesets, read_ruleset

#: The shipped rule-set file.
RULESET_FILE = RULESETS_DIR / "mock-batch-v1.yaml"


def _fields(predicate):
    """Every field a predicate reads, walking its tree to the leaves."""
    if isinstance(predicate, Compare):
        return {predicate.field}
    if isinstance(predicate, AllOf | AnyOf):
        return set().union(*(_fields(child) for child in predicate.predicates))
    assert isinstance(predicate, Not)
    return _fields(predicate.predicate)


def test_the_shipped_rule_set_names_the_plugins_reasoner_id():
    ruleset = read_ruleset(RULESET_FILE)

    assert ruleset.reasoner_id == PLUGIN.registration.reasoner_id
    assert ruleset.ruleset_id == RULESET_ID == "mock-batch-v1"
    assert ruleset.ref == "mock-batch-v1@1"


def test_the_members_rule_set_directory_holds_exactly_that_rule_set():
    loaded = load_rulesets(RULESETS_DIR)

    assert list(loaded) == [RULESET_ID]
    assert loaded[RULESET_ID].origin == RULESET_FILE.name
    assert loaded[RULESET_ID].ruleset == read_ruleset(RULESET_FILE)


def test_every_rule_reads_only_features_every_row_carries():
    ruleset = read_ruleset(RULESET_FILE)
    read = set().union(*(_fields(rule.when) for rule in ruleset.rules))
    # A synthetic day draws every profile, flagged ones included.
    rows = synthetic_day(200, business_date=date(2026, 10, 22), run_tag="rules")
    feature_sets = [score_row(position).feature_set for position in rows]

    assert read == {"feature_set.days_past_due", "feature_set.returned_payments"}
    for field in read:
        name = field.removeprefix("feature_set.")
        assert all(name in features for features in feature_sets), field


@pytest.mark.parametrize(
    ("position", "action", "matched"),
    [
        pytest.param({}, DecisionAction.ALLOW, [], id="current"),
        pytest.param(
            {"balance": 850.0, "days_past_due": 30},
            DecisionAction.CHALLENGE,
            ["flagged-past-due"],
            id="30-days-past-due",
        ),
        pytest.param(
            {"balance": 950.0, "days_past_due": 60},
            DecisionAction.HOLD,
            ["flagged-delinquent", "flagged-past-due"],
            id="60-days-past-due",
        ),
        pytest.param(
            {"returned_payments": 1},
            DecisionAction.HOLD,
            ["flagged-returned-payment"],
            id="a-returned-payment",
        ),
        pytest.param(
            {"balance": 1_200.0, "days_past_due": 90},
            DecisionAction.BLOCK,
            ["flagged-charge-off", "flagged-delinquent", "flagged-past-due"],
            id="90-days-past-due",
        ),
        pytest.param(
            {"balance": 1_200.0, "days_past_due": 120, "returned_payments": 2},
            DecisionAction.BLOCK,
            [
                "flagged-charge-off",
                "flagged-delinquent",
                "flagged-returned-payment",
                "flagged-past-due",
            ],
            id="120-days-past-due-and-returned-payments",
        ),
    ],
)
async def test_the_rules_gate_decides_each_kind_of_flagged_account_as_documented(
    client, row, position, action, matched
):
    flagged = row(1, needs_review=True, **position)
    submission = build_submission(flagged, score_row(flagged))
    assert submission.mode == "gate"

    receipt = await client.submit(submission)
    bundle = await client.get_bundle(receipt.decision_id)

    assert receipt.decision_action == action
    assert receipt.decision_level_rule is None
    record = bundle.gate("rules")
    assert record is not None
    assert record.status == "ran"
    output = record.output
    assert output is not None
    assert "failure" not in output.extra
    assert output.verdict is not None
    snapshot = output.evidence_snapshot
    assert isinstance(snapshot, dict)
    assert snapshot["ruleset_id"] == RULESET_ID
    assert snapshot["matched_rule_ids"] == matched
    assert snapshot["default_applied"] == (not matched)
    assert [ref.locator for ref in output.evidence] == (matched or ["default_action"])
    expected_controls = ["collections_review"] if action == DecisionAction.BLOCK else []
    assert output.verdict.required_controls == expected_controls
