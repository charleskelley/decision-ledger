"""The day's run: every row computed, then submitted on its own, in row order.

``compute_day`` computes each row's drift and assembles its submission, in
row order, each row alone (so the order of a day's rows changes no row's
submission), and refuses a day that repeats an ``event_id`` (so
``run_day`` sends nothing). ``run_day``, through ``LocalClient`` over the
ledger in memory (the mock's plugin and the real rules gate over the
member's rule set):

- submits each row on its own (no batch endpoint), in row order, and reads
  each decided row's bundle back for the rules gate's verdict;
- reports a row the ledger refuses for itself (a 4xx about the submission)
  and goes on;
- stops (``DayRunStopped``, carrying the decisions made until then) when the
  ledger refuses the key (401, 403), answers a server error (5xx), cannot be
  reached, or loses the bundle of a row it decided: a row decided before
  its read failed is kept, from its receipt (the gate's status and any
  failure reason the receipt names), and a row sent but not answered (a 5xx,
  or a transport error after sending) is named ``in_doubt``;
- records a row whose cited rule a ledger serving another version of the
  registration finds does not hold under a HOLD floor, which the report
  counts: not a refusal.

The run's ``summary`` counts each kind of decision (recorded, exceptions)
by action, gate status, and matched rule, with capped samples;
``records`` lists every decision, in row order, then every refusal.
``registration_problem`` checks first that the ledger serves this
registration's version.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from app.client import LocalClient
from app.enforcement.resolver import BYPASS_RULE_NOT_SATISFIED
from core.actions import DecisionAction
from core.exceptions import LedgerError
from core.observation import registration_version
from mock_daily_reasoner.assembler import build_submission
from mock_daily_reasoner.compute import compute_drift
from mock_daily_reasoner.daily import (
    SAMPLE_SIZE,
    DayRunStopped,
    compute_day,
    registration_problem,
    run_day,
)
from mock_daily_reasoner.eval.driver import rules_gate_plugin
from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION
from mock_daily_reasoner.synthetic import synthetic_day
from tests._memory_ledger import memory_framework

#: Rows in the tests' synthetic day (the default seed): a few are exceptions.
COUNT = 100


class _RaisingGate:
    """A rules gate that raises: the ledger fails it closed."""

    async def evaluate(self, request):
        msg = "the test's rules gate failed"
        raise RuntimeError(msg)

    def close(self) -> None:
        """Nothing to release."""


def _day(business_date, *, tag="run"):
    return synthetic_day(COUNT, business_date=business_date, run_tag=tag)


# ---------------------------------------------------------------------------
# The computed day
# ---------------------------------------------------------------------------


def test_compute_day_records_rows_within_tolerance_and_gates_the_rest(settlement):
    rows = [
        settlement(1),  # settled exactly
        settlement(2, target=5_000.0, actual=4_850.0),  # 3% short
        settlement(3, target=10_000.0, actual=10_099.0),  # 0.99% over
        settlement(4, target=12_000.0, actual=9_000.0),  # 25% short
    ]

    computed = compute_day(rows)

    assert [c.settlement for c in computed] == rows
    assert [c.submission.mode for c in computed] == ["record", "gate", "record", "gate"]
    assert [c.is_exception for c in computed] == [False, True, False, True]
    for c in computed:
        assert c.drift == compute_drift(c.settlement)
        assert c.submission == build_submission(c.settlement, c.drift)


def test_a_day_assembles_each_row_the_same_whatever_order_its_rows_arrive_in(
    business_date,
):
    rows = _day(business_date)

    in_order = {c.settlement.event_id: c.submission for c in compute_day(rows)}
    reversed_order = {
        c.settlement.event_id: c.submission for c in compute_day(rows[::-1])
    }

    assert reversed_order == in_order


async def test_a_day_repeating_an_event_id_is_refused_before_anything_is_sent(
    ledger, refusing_client, settlement
):
    rows = [settlement(1), settlement(2), settlement(1, account="acct-elsewhere")]
    client = refusing_client()

    with pytest.raises(ValueError, match="two rows share the event id 'set-test-0001'"):
        compute_day(rows)
    with pytest.raises(ValueError, match="two rows share the event id 'set-test-0001'"):
        await run_day(rows, client)

    assert client.submitted == []
    assert ledger.store.rows == {}


# ---------------------------------------------------------------------------
# The run, through the ledger in memory
# ---------------------------------------------------------------------------


async def test_run_day_submits_each_row_on_its_own_in_row_order_and_reads_it_back(
    ledger, refusing_client, business_date
):
    rows = _day(business_date)
    computed = compute_day(rows)
    client = refusing_client()  # refuses nothing, and has no batch endpoint

    run = await run_day(rows, client)

    assert client.submitted == [row.event_id for row in rows]
    assert client.read == [d.decision_id for d in run.decisions]
    assert [d.event_id for d in run.decisions] == [row.event_id for row in rows]
    assert [d.mode for d in run.decisions] == [c.submission.mode for c in computed]
    assert [d.proposed_action for d in run.decisions] == [
        c.submission.proposed_action for c in computed
    ]
    assert (run.rows, run.decided, run.undecided, run.refused) == (
        COUNT,
        COUNT,
        0,
        [],
    )
    # Each row once: the ledger does not deduplicate, so nothing went twice.
    assert len(ledger.store.rows) == COUNT
    assert all(run.action_of(row.event_id) is not None for row in rows)


async def test_rows_the_ledger_refuses_are_reported_and_the_run_goes_on(
    refusing_client, business_date
):
    rows = _day(business_date)
    computed = compute_day(rows)
    # The first rows the ledger refuses: the day's first recorded row and its
    # first exceptions among them, more than a sample holds.
    refused = [row.event_id for row in rows[: SAMPLE_SIZE + 2]]
    assert {c.submission.mode for c in computed[: SAMPLE_SIZE + 2]} == {
        "record",
        "gate",
    }
    client = refusing_client(*refused)

    run = await run_day(rows, client)

    assert client.submitted == [row.event_id for row in rows]
    assert [(r.event_id, r.status, r.code) for r in run.refused] == [
        (event_id, client.status, client.code) for event_id in refused
    ]
    assert all(r.event_id in r.detail for r in run.refused)
    assert [d.event_id for d in run.decisions] == [
        row.event_id for row in rows[SAMPLE_SIZE + 2 :]
    ]
    assert all(run.action_of(event_id) is None for event_id in refused)
    assert (run.decided, run.undecided) == (COUNT - len(refused), len(refused))
    summary = run.summary()
    assert summary["refused"] == len(refused)
    assert summary["refused_sample"] == [r.as_dict() for r in run.refused[:SAMPLE_SIZE]]


async def test_a_failed_bundle_read_stops_the_run_with_the_decisions_made_so_far(
    ledger, refusing_client, business_date
):
    rows = _day(business_date)
    lost = rows[5].event_id
    client = refusing_client(lose_bundle=[lost])

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client)

    cause = stopped.value.__cause__
    assert isinstance(cause, LedgerError)
    assert (cause.status, cause.code) == (500, "INTERNAL")
    run = stopped.value.run
    assert run.stopped == f"LedgerError: {cause}" == str(stopped.value)
    # The ledger had decided the row whose bundle could not be read: the run
    # keeps it, from its receipt, so the operator knows every row decided.
    assert [d.event_id for d in run.decisions] == [row.event_id for row in rows[:6]]
    assert (run.decided, run.undecided) == (6, COUNT - 6)
    assert [d.bundle_read for d in run.decisions] == [True] * 5 + [False]
    kept = run.decisions[-1]
    [stored] = [b for b in ledger.store.bundles() if b.decision_id == kept.decision_id]
    assert kept.decision_action == stored.decision_action
    assert kept.mode == stored.submission.mode
    assert kept.proposed_action == stored.submission.proposed_action
    assert kept.gate_status == "ran"
    assert (kept.matched_rules, kept.default_applied, kept.failure) == ((), None, None)
    assert run.records()[5] == {"kind": "decision", **kept.as_dict()}
    assert run.summary()["stopped"] == run.stopped
    # Nothing was sent after the stop.
    assert client.submitted == [row.event_id for row in rows[:6]]
    assert len(ledger.store.rows) == 6


async def test_an_unreachable_ledger_stops_the_run_with_the_decisions_made_so_far(
    ledger, refusing_client, business_date
):
    rows = _day(business_date)
    client = refusing_client(unreachable_from=rows[3].event_id)

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client)

    assert isinstance(stopped.value.__cause__, httpx.ConnectError)
    run = stopped.value.run
    assert run.stopped == "ConnectError: the test's ledger cannot be reached"
    assert [d.event_id for d in run.decisions] == [row.event_id for row in rows[:3]]
    assert (run.decided, run.undecided, run.refused) == (3, COUNT - 3, [])
    # Never reached the ledger: nothing is in doubt.
    assert run.in_doubt is None
    assert client.submitted == [row.event_id for row in rows[:4]]
    assert len(ledger.store.rows) == 3


@pytest.mark.parametrize(
    ("status", "code"),
    [
        pytest.param(401, "UNAUTHENTICATED", id="no-key"),
        pytest.param(403, "REASONER_MISMATCH", id="another-reasoners-key"),
        pytest.param(403, "REASONER_KEY_REQUIRED", id="the-operator-key"),
    ],
)
async def test_a_refused_key_stops_the_run_at_the_first_row(
    ledger, refusing_client, business_date, status, code
):
    rows = _day(business_date)
    # The ledger refuses the key, so it would refuse every row.
    client = refusing_client(*(row.event_id for row in rows), status=status, code=code)

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client)

    cause = stopped.value.__cause__
    assert isinstance(cause, LedgerError)
    assert (cause.status, cause.code) == (status, code)
    run = stopped.value.run
    assert run.stopped == f"LedgerError: {cause}"
    assert (run.decided, run.undecided, run.refused, run.in_doubt) == (
        0,
        COUNT,
        [],
        None,
    )
    assert client.submitted == [rows[0].event_id]
    assert ledger.store.rows == {}


async def test_a_server_error_stops_the_run_and_names_the_row_in_doubt(
    ledger, refusing_client, business_date
):
    rows = _day(business_date)
    failing = rows[3].event_id
    client = refusing_client(failing, status=502, code="BAD_GATEWAY")

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client)

    run = stopped.value.run
    assert run.stopped == f"LedgerError: {stopped.value.__cause__}"
    # A 5xx may follow a row the ledger decided: it is in doubt, not refused.
    assert run.in_doubt == failing
    assert run.refused == []
    assert [d.event_id for d in run.decisions] == [row.event_id for row in rows[:3]]
    assert run.summary()["in_doubt"] == failing
    assert client.submitted == [row.event_id for row in rows[:4]]


async def test_a_row_sent_and_not_answered_is_named_in_doubt(
    ledger, refusing_client, business_date
):
    rows = _day(business_date)
    unanswered = rows[4].event_id
    client = refusing_client(unanswered=[unanswered])

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client)

    assert isinstance(stopped.value.__cause__, httpx.ReadTimeout)
    run = stopped.value.run
    assert run.stopped.startswith("ReadTimeout: ")
    assert run.in_doubt == unanswered
    assert [d.event_id for d in run.decisions] == [row.event_id for row in rows[:4]]
    assert (run.decided, run.undecided) == (4, COUNT - 4)
    # The ledger did decide the row in doubt: it must be looked up, not resent.
    assert len(ledger.store.rows) == 5
    assert [b.submission.event_id for b in ledger.store.bundles()][-1] == unanswered


async def test_a_bundle_read_lost_over_the_transport_keeps_the_decided_row(
    ledger, refusing_client, business_date
):
    rows = _day(business_date)
    lost = rows[2].event_id
    client = refusing_client(lose_bundle=[lost], bundle_unreachable=True)

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client)

    assert isinstance(stopped.value.__cause__, httpx.ReadTimeout)
    run = stopped.value.run
    assert [d.event_id for d in run.decisions] == [row.event_id for row in rows[:3]]
    assert [d.bundle_read for d in run.decisions] == [True, True, False]
    # Decided and kept: not in doubt.
    assert run.in_doubt is None
    assert len(ledger.store.rows) == 3


async def test_a_lost_bundle_keeps_the_failure_the_receipt_names(
    ledger, refusing_client, business_date
):
    rules = dataclasses.replace(ledger.gates["rules"], gate=_RaisingGate())
    inner = LocalClient(
        dataclasses.replace(ledger, gates={**ledger.gates, "rules": rules})
    )
    rows = _day(business_date)
    lost = rows[1].event_id
    client = refusing_client(lose_bundle=[lost], inner=inner)

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client)

    run = stopped.value.run
    kept = run.decisions[-1]
    assert (kept.event_id, kept.bundle_read) == (lost, False)
    # The mandatory gate failed on it: the receipt's gate summary says so.
    assert kept.gate_status == "failed"
    assert kept.failure == "raised RuntimeError: the test's rules gate failed"
    assert kept.decision_action == DecisionAction.HOLD
    assert (kept.matched_rules, kept.default_applied) == ((), None)


async def test_a_cited_rule_the_ledger_finds_not_holding_is_floored_and_counted(
    stricter_ledger, business_date
):
    rows = _day(business_date)
    # The rows the mock records that the stricter ledger's rule does not cover.
    disputed = {
        c.settlement.event_id
        for c in compute_day(rows)
        if not c.is_exception and c.drift.abs_drift_pct >= 0.5
    }
    assert disputed

    run = await run_day(rows, LocalClient(stricter_ledger))

    assert (run.decided, run.refused) == (COUNT, [])  # floored, not refused
    for outcome in run.recorded:
        if outcome.event_id in disputed:
            assert outcome.bypass_rule_holds is False
            assert outcome.decision_level_rule == BYPASS_RULE_NOT_SATISFIED
            assert outcome.decision_action == DecisionAction.HOLD
            assert outcome.raised
        else:
            assert outcome.bypass_rule_holds is True
            assert outcome.decision_action == DecisionAction.ALLOW
        # The rules gate ran all the same, and permitted its default.
        assert (outcome.gate_status, outcome.default_applied) == ("ran", True)
    recorded = run.summary()["recorded"]
    assert recorded["cited_rule_not_held"] == recorded["raised"] == len(disputed)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def test_the_report_counts_each_kind_by_action_gate_status_and_rule(
    client, business_date
):
    rows = _day(business_date, tag="report")
    computed = compute_day(rows)
    recorded = [c for c in computed if not c.is_exception]
    exceptions = [c for c in computed if c.is_exception]
    # From 20% blocked, from 5% held (blocked ones included), from 1% challenged.
    severe = sum(c.drift.abs_drift_pct >= 20.0 for c in exceptions)
    major = sum(c.drift.abs_drift_pct >= 5.0 for c in exceptions)

    run = await run_day(rows, client)
    summary = run.summary()

    assert (summary["rows"], summary["decided"], summary["undecided"]) == (
        COUNT,
        COUNT,
        0,
    )
    assert summary["stopped"] is None
    assert summary["recorded"] == {
        "decided": len(recorded),
        "actions": {"ALLOW": len(recorded)},
        "gate_status": {"ran": len(recorded)},
        "matched_rules": {},
        "default_applied": len(recorded),
        "failures": 0,
        "failure_sample": [],
        "sample": [d.as_dict() for d in run.recorded[:SAMPLE_SIZE]],
        "raised": 0,
        "cited_rule_not_held": 0,
    }
    # A synthetic day's first rows hold an exception of every severity, so
    # every count below is at least one.
    assert summary["exceptions"] == {
        "decided": len(exceptions),
        "actions": {
            "BLOCK": severe,
            "HOLD": major - severe,
            "CHALLENGE": len(exceptions) - major,
        },
        "gate_status": {"ran": len(exceptions)},
        "matched_rules": {
            "drift-severe": severe,
            "drift-major": major,
            "drift-minor": len(exceptions),
        },
        "default_applied": 0,
        "failures": 0,
        "failure_sample": [],
        "sample": [d.as_dict() for d in run.exceptions[:SAMPLE_SIZE]],
    }
    assert len(summary["recorded"]["sample"]) == SAMPLE_SIZE
    assert (summary["refused"], summary["refused_sample"]) == (0, [])
    assert json.loads(json.dumps(summary)) == summary


async def test_records_lists_every_decision_in_row_order_then_every_refusal(
    refusing_client, business_date
):
    rows = _day(business_date)
    refused = [rows[1].event_id, rows[3].event_id]
    run = await run_day(rows, refusing_client(*refused))

    records = run.records()

    assert [r["kind"] for r in records] == ["decision"] * (COUNT - 2) + ["refused"] * 2
    assert [r["event_id"] for r in records] == [
        *(row.event_id for row in rows if row.event_id not in refused),
        *refused,
    ]
    assert records[0] == {"kind": "decision", **run.decisions[0].as_dict()}
    assert records[-1] == {"kind": "refused", **run.refused[-1].as_dict()}
    assert set(records[0]) == {
        "kind",
        "event_id",
        "decision_id",
        "mode",
        "proposed_action",
        "decision_action",
        "decision_level_rule",
        "bypass_rule_holds",
        "gate_status",
        "matched_rules",
        "default_applied",
        "failure",
        "bundle_read",
    }
    assert json.loads(json.dumps(records)) == records


# ---------------------------------------------------------------------------
# The registration the ledger serves
# ---------------------------------------------------------------------------


async def test_a_ledger_serving_this_registration_is_no_problem(client):
    assert await registration_problem(client) is None


async def test_a_ledger_serving_another_version_of_the_registration_is_a_problem(
    stricter_ledger, stricter_registration
):
    source = stricter_ledger.registry.entry(MOCK_DAILY_REGISTRATION.reasoner_id).source

    problem = await registration_problem(LocalClient(stricter_ledger))

    assert problem is not None
    assert registration_version(stricter_registration) in problem
    assert registration_version(MOCK_DAILY_REGISTRATION) in problem
    assert source.startswith("file:")
    assert f"({source})" in problem


async def test_a_ledger_that_does_not_register_the_mock_answers_not_found():
    ledger = memory_framework((), [rules_gate_plugin()])

    with pytest.raises(LedgerError) as missing:
        await registration_problem(LocalClient(ledger))

    assert (missing.value.status, missing.value.code) == (404, "REASONER_NOT_FOUND")
