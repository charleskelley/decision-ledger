"""The rules gate, bound mandatory, on every decision: recorded ones too.

Through the ledger in memory, a synthetic day of 1,000 accounts shows the
rules gate ran on every decision, recorded or gated: on a recorded row it
matched no rule and permitted its default, so the record kept its ALLOW; on
an exception at least one rule matched and decided it; and every decision
replays to the action it was decided with. The gate's verdict joins the
combinator with the record's own action: a rule that fires on a recorded
row (a rule set with one more rule than the member's) raises its ALLOW to
HOLD, while the cited rule still holds, and the day's report counts every
row it raised. A rules gate that fails (it raises, or the ledger has not
staged the rule set) holds every decision, recorded ones too, and the
report counts the failures.
"""

from __future__ import annotations

import dataclasses

import yaml

from app.client import LocalClient
from core.actions import DecisionAction
from core.submission import BypassCitation
from mock_daily_reasoner.assembler import build_submission
from mock_daily_reasoner.compute import compute_drift
from mock_daily_reasoner.daily import SAMPLE_SIZE, compute_day, run_day
from mock_daily_reasoner.plugin import PLUGIN, RULESETS_DIR
from mock_daily_reasoner.synthetic import synthetic_day
from rules_gate import RulesGatePlugin
from rules_gate.settings import RulesGateSettings
from tests._memory_ledger import memory_framework

#: A rule that fires within tolerance: from a 0.5% drift, hold the settlement.
WATCH_RULE = {
    "rule_id": "drift-watch",
    "description": "Drifted 0.5% or more from the target; hold it for review.",
    "when": {
        "kind": "compare",
        "field": "feature_set.abs_drift_pct",
        "op": "ge",
        "value": 0.5,
    },
    "action": "HOLD",
}


def _with_watch_rule(tmp_path):
    """The rules gate over the member's rule set with ``WATCH_RULE`` added.

    The same ``ruleset_id`` (``mock-daily-v1``) and ``reasoner_id``, in a
    rule-set directory of its own.
    """
    shipped = RULESETS_DIR / "mock-daily-v1.yaml"
    ruleset = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    ruleset["rules"].append(WATCH_RULE)
    directory = tmp_path / "rulesets"
    directory.mkdir()
    (directory / shipped.name).write_text(yaml.safe_dump(ruleset), encoding="utf-8")
    return RulesGatePlugin(RulesGateSettings(rulesets_dir=str(directory)))


class _RaisingGate:
    """A rules gate that raises: the ledger fails it closed."""

    async def evaluate(self, request):
        msg = "the test's rules gate failed"
        raise RuntimeError(msg)

    def close(self) -> None:
        """Nothing to release."""


async def test_the_rules_gate_runs_on_every_decision_of_a_1000_row_day_all_replay(
    client, business_date
):
    rows = synthetic_day(1_000, business_date=business_date, run_tag="day")

    run = await run_day(rows, client)

    assert (run.rows, run.decided, run.undecided) == (1_000, 1_000, 0)
    assert run.recorded
    assert run.exceptions
    for outcome in run.decisions:
        bundle = await client.get_bundle(outcome.decision_id)
        record = bundle.gate("rules")
        assert record is not None
        assert (record.status, record.mandatory) == ("ran", True)
        output = record.output
        assert output is not None
        assert output.verdict is not None
        assert "failure" not in output.extra
        snapshot = output.evidence_snapshot
        assert isinstance(snapshot, dict)
        if bundle.submission.mode == "record":
            # Within tolerance: no rule matched, the gate permitted its
            # default, and the record kept its ALLOW.
            assert bundle.bypass_rule_holds is True
            assert (snapshot["matched_rule_ids"], snapshot["default_applied"]) == (
                [],
                True,
            )
            assert bundle.decision_action == DecisionAction.ALLOW
        else:
            # An exception: decided by the rules that matched.
            assert snapshot["matched_rule_ids"]
            assert snapshot["default_applied"] is False
            assert bundle.decision_action == record.enforced_action
        assert bundle.decision_level_rule is None
        assert outcome.matched_rules == tuple(snapshot["matched_rule_ids"])
        assert (outcome.gate_status, outcome.failure) == ("ran", None)
        replay = await client.replay(outcome.decision_id)
        assert replay.matched
        assert replay.replayed_action == replay.original_action
        assert replay.original_action == bundle.decision_action.value


async def test_a_rule_that_fires_on_a_recorded_row_raises_its_allow_to_hold(
    tmp_path, settlement
):
    client = LocalClient(memory_framework([PLUGIN], [_with_watch_rule(tmp_path)]))
    row = settlement(1, target=10_000.0, actual=10_060.0)  # 0.6% over
    submission = build_submission(row, compute_drift(row))
    assert submission.mode == "record"
    assert submission.proposed_action == DecisionAction.ALLOW
    assert submission.bypass == BypassCitation(
        rule_id="within-tolerance", rule_version="1"
    )

    receipt = await client.submit(submission)
    bundle = await client.get_bundle(receipt.decision_id)

    assert receipt.mode == "record"
    assert receipt.bypass_rule_holds is True  # the cited rule held
    assert receipt.decision_level_rule is None
    assert receipt.decision_action == DecisionAction.HOLD
    record = bundle.gate("rules")
    assert record is not None
    assert (record.status, record.mandatory) == ("ran", True)
    assert record.output is not None
    assert record.output.verdict is not None
    assert record.output.verdict.permitted_actions == [DecisionAction.HOLD]
    assert record.output.evidence_snapshot["matched_rule_ids"] == ["drift-watch"]
    assert record.enforced_action == DecisionAction.HOLD
    # The record's own action and the gate's joined the combinator.
    assert bundle.override_log[-1] == (
        "combinator: most_restrictive(proposed=ALLOW, mandatory:rules=HOLD) → HOLD"
    )
    replay = await client.replay(receipt.decision_id)
    assert replay.matched
    assert replay.replayed_action == replay.original_action == "HOLD"


async def test_the_days_report_counts_every_recorded_row_the_gate_raised(
    tmp_path, business_date
):
    client = LocalClient(memory_framework([PLUGIN], [_with_watch_rule(tmp_path)]))
    rows = synthetic_day(200, business_date=business_date, run_tag="watch")
    watched = {
        c.settlement.event_id
        for c in compute_day(rows)
        if not c.is_exception and c.drift.abs_drift_pct >= 0.5
    }

    run = await run_day(rows, client)

    # Only a record can be raised: an exception had no action of its own.
    raised = [d for d in run.decisions if d.raised]
    assert watched
    assert {d.event_id for d in raised} == watched
    for outcome in raised:
        assert outcome.proposed_action == DecisionAction.ALLOW
        assert outcome.decision_action == DecisionAction.HOLD
        assert outcome.bypass_rule_holds is True
        assert outcome.matched_rules == ("drift-watch",)
        assert outcome.default_applied is False
    recorded = run.summary()["recorded"]
    assert recorded["raised"] == len(watched)
    assert recorded["actions"] == {
        "ALLOW": len(run.recorded) - len(watched),
        "HOLD": len(watched),
    }
    assert recorded["matched_rules"] == {"drift-watch": len(watched)}
    assert recorded["default_applied"] == len(run.recorded) - len(watched)
    assert recorded["cited_rule_not_held"] == 0


async def test_a_rules_gate_that_raises_holds_every_decision_recorded_ones_too(
    ledger, business_date
):
    rules = dataclasses.replace(ledger.gates["rules"], gate=_RaisingGate())
    client = LocalClient(
        dataclasses.replace(ledger, gates={**ledger.gates, "rules": rules})
    )
    rows = synthetic_day(60, business_date=business_date, run_tag="raising")

    run = await run_day(rows, client)

    assert run.recorded
    assert run.exceptions
    for outcome in run.decisions:
        record = (await client.get_bundle(outcome.decision_id)).gate("rules")
        assert record is not None
        assert (record.status, record.mandatory, record.output) == (
            "failed",
            True,
            None,
        )
        assert record.reason == "raised RuntimeError: the test's rules gate failed"
        assert outcome.decision_action == DecisionAction.HOLD
        assert (outcome.gate_status, outcome.failure) == ("failed", record.reason)
        assert (outcome.matched_rules, outcome.default_applied) == ((), None)
    # A record's cited rule still held: the gate alone held it.
    assert {d.bypass_rule_holds for d in run.recorded} == {True}
    summary = run.summary()
    recorded, exceptions = summary["recorded"], summary["exceptions"]
    assert recorded["failures"] == recorded["raised"] == len(run.recorded)
    assert recorded["gate_status"] == {"failed": len(run.recorded)}
    assert len(recorded["failure_sample"]) == SAMPLE_SIZE
    assert exceptions["failures"] == len(run.exceptions)
    assert exceptions["actions"] == {"HOLD": len(run.exceptions)}


async def test_without_the_rule_set_every_decision_holds_recorded_ones_too(
    tmp_path, business_date
):
    empty = tmp_path / "rulesets"
    empty.mkdir()
    gate = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(empty)))
    client = LocalClient(memory_framework([PLUGIN], [gate]))
    rows = synthetic_day(60, business_date=business_date, run_tag="unstaged")

    run = await run_day(rows, client)

    assert run.recorded
    assert run.exceptions
    for outcome in run.decisions:
        assert outcome.decision_action == DecisionAction.HOLD
        assert (outcome.gate_status, outcome.failure) == ("ran", "unknown_ruleset")
        assert (outcome.matched_rules, outcome.default_applied) == ((), None)
    record = (await client.get_bundle(run.recorded[0].decision_id)).gate("rules")
    assert record is not None
    assert record.output is not None
    assert record.output.verdict is None
    assert record.output.extra["failure"] == "unknown_ruleset"
    summary = run.summary()
    recorded, exceptions = summary["recorded"], summary["exceptions"]
    assert recorded["failures"] == recorded["raised"] == len(run.recorded)
    assert exceptions["failures"] == len(run.exceptions)
