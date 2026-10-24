"""The day's run: records in batch requests, exceptions through gate-mode submit.

``plan_day`` splits a scored day into batch requests (every recorded row,
in row order, at most ``chunk_size`` lines each, 1 to 10,000) with the
first exception riding in the first request at its row's place (the
provoked line, unless provoking is off), and the exceptions, in row order;
``score_rows`` refuses a day that repeats an ``event_id``. ``run_day``,
through ``LocalClient`` over the ledger in memory (the mock's plugin and the
real rules gate over the member's rule set):

- sends the planned requests and records every covered row under the rule
  it cites, writing each request's decisions in one round trip;
- has exactly one line rejected, the provoked one, with
  ``BATCH_RECORD_ONLY``, and decides that row once, through ``submit``, like
  every other exception, which the rules gate decides;
- decides every row, so a recorded line and an exception replay to the same
  action, and an exception's evidence verifies against the rule set the
  gate escrowed;
- reports an exception the ledger refuses and goes on; reports a provoked
  line the ledger rejected for another reason and never submits it; stops
  on a refused or failed batch request, carrying what the ledger answered
  until then (``DayRunStopped``); holds every exception whose rules gate
  failed (without the rule set, or raising) and counts the failures;
- checks first that the ledger serves this registration's version
  (``registration_problem``).

A cited bypass rule that does not hold is not a rejection: the ledger
records the line under a HOLD floor (``bypass_rule_not_satisfied``). The
run's ``summary`` caps its lists, always shows the provoked rejection (and
marks none provoked when nothing was), and counts such lines;
``records`` lists every line, then every exception, then every refusal.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from app.api.models import BatchLineResult
from app.auth import Principal
from app.client import LocalClient
from app.enforcement.resolver import BYPASS_RULE_NOT_SATISFIED
from core.actions import DecisionAction
from core.exceptions import LedgerError
from core.observation import registration_version
from core.submission import BypassCitation
from mock_batch_reasoner.assembler import build_submission
from mock_batch_reasoner.batch import (
    BATCH_RECORD_ONLY,
    MAX_BATCH_LINES,
    SAMPLE_SIZE,
    DayRun,
    DayRunStopped,
    ExceptionOutcome,
    LineOutcome,
    Refusal,
    plan_day,
    registration_problem,
    run_day,
    score_rows,
)
from mock_batch_reasoner.eval.driver import rules_gate_plugin
from mock_batch_reasoner.plugin import PLUGIN
from mock_batch_reasoner.registry import MOCK_BATCH_REGISTRATION, registration_yaml
from mock_batch_reasoner.scorer import score_row
from mock_batch_reasoner.synthetic import synthetic_day
from rules_gate import RulesGatePlugin
from rules_gate.settings import RulesGateSettings
from tests._memory_ledger import memory_framework

#: Rows in the tests' synthetic day (the default seed): a few are exceptions.
COUNT = 300
#: Lines per batch request in the tests: the day takes three requests.
CHUNK = 100


def _scored(business_date, *, tag="plan"):
    return score_rows(synthetic_day(COUNT, business_date=business_date, run_tag=tag))


def _lines(plan):
    """Every batch line of a plan, in the order sent."""
    return [submission for chunk in plan.chunks for submission in chunk]


async def _run(client, business_date, *, tag="run"):
    """The tests' day, its plan, and what the ledger answered for it."""
    rows = synthetic_day(COUNT, business_date=business_date, run_tag=tag)
    plan = plan_day(score_rows(rows), chunk_size=CHUNK)
    run = await run_day(rows, client, chunk_size=CHUNK)
    return rows, plan, run


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_plan_day_chunks_the_recorded_rows_in_row_order(business_date):
    scored = _scored(business_date)

    plan = plan_day(scored, chunk_size=CHUNK)

    assert [len(chunk) for chunk in plan.chunks] == [100, 100, 97]
    ids = [submission.event_id for submission in _lines(plan)]
    recorded = {s.row.event_id for s in scored if not s.row.needs_review}
    assert ids == sorted(ids)  # row ids are zero-padded: row order
    assert len(ids) == len(recorded) + 1
    assert set(ids) == recorded | {plan.provoked}
    # At the default size, the ledger's limit, one request holds every line.
    [request] = plan_day(scored).chunks
    assert list(request) == _lines(plan)


def test_the_provoked_line_is_the_first_exception_at_its_row_place_in_the_first_chunk(
    business_date,
):
    scored = _scored(business_date)

    plan = plan_day(scored, chunk_size=CHUNK)

    first = next(s for s in scored if s.row.needs_review)
    assert plan.provoked == first.row.event_id == "row-plan-000005"
    assert plan.chunks[0][scored.index(first)] == first.submission
    assert [s for s in _lines(plan) if s.mode == "gate"] == [first.submission]


def test_the_exceptions_are_every_flagged_row_in_row_order(business_date):
    scored = _scored(business_date)

    plan = plan_day(scored, chunk_size=CHUNK)

    assert plan.exceptions == tuple(s.submission for s in scored if s.row.needs_review)
    assert len(plan.exceptions) > 2
    assert {submission.mode for submission in plan.exceptions} == {"gate"}
    assert plan.exceptions[0].event_id == plan.provoked


def test_without_provoking_no_chunk_carries_a_gate_mode_line(business_date):
    scored = _scored(business_date)

    plan = plan_day(scored, chunk_size=CHUNK, provoke_record_only=False)

    assert plan.provoked is None
    assert {submission.mode for submission in _lines(plan)} == {"record"}
    assert len(_lines(plan)) == len(scored) - len(plan.exceptions)
    assert plan.exceptions == plan_day(scored, chunk_size=CHUNK).exceptions


def test_a_day_without_exceptions_provokes_nothing(row):
    plan = plan_day(score_rows([row(n) for n in range(1, 4)]))

    assert plan.provoked is None
    assert plan.exceptions == ()
    assert [len(chunk) for chunk in plan.chunks] == [3]


@pytest.mark.parametrize("chunk_size", [0, MAX_BATCH_LINES + 1])
def test_plan_day_refuses_a_chunk_size_outside_one_to_the_ledgers_limit(
    business_date, chunk_size
):
    with pytest.raises(ValueError, match=f"from 1 to 10000, got {chunk_size}"):
        plan_day(_scored(business_date), chunk_size=chunk_size)


def test_score_rows_refuses_two_rows_sharing_an_event_id(row):
    with pytest.raises(ValueError, match="two rows share the event id 'row-test-0001'"):
        score_rows([row(1), row(2), row(1, account="acct-elsewhere")])


# ---------------------------------------------------------------------------
# The run, through the ledger in memory
# ---------------------------------------------------------------------------


async def test_run_day_sends_the_planned_requests_and_records_every_covered_row(
    client, business_date
):
    _, plan, run = await _run(client, business_date)

    assert run.requests == [
        {"lines": 100, "recorded": 99, "rejected": 1},
        {"lines": 100, "recorded": 100, "rejected": 0},
        {"lines": 97, "recorded": 97, "rejected": 0},
    ]
    assert [o.result.event_id for o in run.lines] == [s.event_id for s in _lines(plan)]
    assert [o.request for o in run.lines] == [
        request for request, chunk in enumerate(plan.chunks, 1) for _ in chunk
    ]
    planned = {s.event_id: s for s in _lines(plan) if s.mode == "record"}
    assert {o.result.event_id for o in run.recorded} == set(planned)
    for outcome in run.recorded:
        submission = planned[outcome.result.event_id]
        assert outcome.result.decision_action == submission.proposed_action
        assert outcome.result.bypass_rule_holds is True


async def test_the_ledger_rejects_only_the_provoked_line_as_record_only(
    client, business_date
):
    _, plan, run = await _run(client, business_date)

    [rejection] = run.rejected
    assert rejection.result.code == BATCH_RECORD_ONLY == "BATCH_RECORD_ONLY"
    assert rejection.result.event_id == run.provoked == plan.provoked
    assert (rejection.request, rejection.result.line) == (1, 5)  # its row's place
    assert rejection.result.decision_id is None
    assert run.summary()["batch"]["rejected_codes"] == {"BATCH_RECORD_ONLY": 1}
    # Every other line was recorded, under a rule that held.
    assert [o for o in run.lines if o.result.status != "recorded"] == [rejection]
    assert {o.result.bypass_rule_holds for o in run.recorded} == {True}


async def test_every_exception_is_decided_by_the_rules_gate_through_submit(
    client, business_date
):
    _, plan, run = await _run(client, business_date)

    assert [e.event_id for e in run.exceptions] == [s.event_id for s in plan.exceptions]
    assert run.refused == []
    for exception in run.exceptions:
        bundle = await client.get_bundle(exception.decision_id)
        assert bundle.submission.event_id == exception.event_id
        assert bundle.submission.mode == "gate"
        assert bundle.decision_action == exception.decision_action
        record = bundle.gate("rules")
        assert record is not None
        assert record.status == "ran"
        output = record.output
        assert output is not None
        assert output.verdict is not None
        assert "failure" not in output.extra
        snapshot = output.evidence_snapshot
        assert isinstance(snapshot, dict)
        assert list(exception.matched_rules) == snapshot["matched_rule_ids"]
        assert exception.default_applied == snapshot["default_applied"]
        assert exception.failure is None
    # The day holds both of the rule set's outcomes: rules matched, or none.
    assert any(e.matched_rules for e in run.exceptions)
    assert any(e.default_applied for e in run.exceptions)


async def test_the_provoked_row_is_decided_exactly_once_through_submit(
    ledger, client, business_date
):
    _, plan, run = await _run(client, business_date)

    [bundle] = [
        b for b in ledger.store.bundles() if b.submission.event_id == plan.provoked
    ]
    assert bundle.submission.mode == "gate"
    [provoked] = [e for e in run.exceptions if e.provoked]
    assert (provoked.event_id, provoked.decision_id) == (
        plan.provoked,
        bundle.decision_id,
    )
    assert run.action_of(plan.provoked) == bundle.decision_action


async def test_every_row_is_decided_and_each_request_is_written_in_one_round_trip(
    ledger, client, business_date
):
    rows, _, run = await _run(client, business_date)

    assert (run.rows, run.decided, run.undecided) == (COUNT, COUNT, 0)
    assert all(run.action_of(position.event_id) is not None for position in rows)
    # Each row once: the ledger does not deduplicate, so nothing went twice.
    assert len(ledger.store.rows) == len(rows)
    assert ledger.store.write_many_calls == [r["recorded"] for r in run.requests]


async def test_a_cited_rule_that_does_not_hold_is_recorded_as_hold_not_rejected(
    client, row
):
    flagged = row(1, needs_review=True)
    cited = build_submission(flagged, score_row(flagged)).model_copy(
        update={
            "mode": "record",
            "proposed_action": DecisionAction.ALLOW,
            "bypass": BypassCitation(rule_id="current-account", rule_version="1"),
        }
    )

    result = await client.submit_batch([cited])

    assert (result.recorded, result.rejected) == (1, 0)
    [line] = result.results
    assert (line.status, line.code) == ("recorded", None)
    assert line.decision_action == DecisionAction.HOLD
    assert line.bypass_rule_holds is False
    bundle = await client.get_bundle(line.decision_id)
    assert bundle.decision_level_rule == BYPASS_RULE_NOT_SATISFIED
    assert BYPASS_RULE_NOT_SATISFIED == "bypass_rule_not_satisfied"
    assert bundle.bypass_check is not None
    assert bundle.bypass_check.observed["feature_set.needs_review"] is True


async def test_an_exception_the_ledger_refuses_is_reported_and_the_run_goes_on(
    refusing_client, business_date
):
    rows = synthetic_day(COUNT, business_date=business_date, run_tag="run")
    plan = plan_day(score_rows(rows), chunk_size=CHUNK)
    first, refused, *later = (s.event_id for s in plan.exceptions)
    client = refusing_client(refused)

    run = await run_day(rows, client, chunk_size=CHUNK)

    assert later  # exceptions after the refused one
    assert client.submitted == [first, refused, *later]
    [refusal] = run.refused
    assert (refusal.event_id, refusal.status, refusal.code) == (
        refused,
        client.status,
        client.code,
    )
    assert refused in refusal.detail
    assert [e.event_id for e in run.exceptions] == [first, *later]
    assert run.action_of(refused) is None
    assert (run.decided, run.undecided) == (COUNT - 1, 1)
    summary = run.summary()["exceptions"]
    assert summary["refused"] == 1
    assert summary["refused_sample"] == [refusal.as_dict()]


async def test_a_refused_batch_request_stops_the_run(ledger, business_date):
    rows = synthetic_day(COUNT, business_date=business_date, run_tag="run")
    other = LocalClient(ledger, principal=Principal.for_reasoner("other-reasoner"))

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, other, chunk_size=CHUNK)

    refused = stopped.value.__cause__
    assert isinstance(refused, LedgerError)
    assert (refused.status, refused.code) == (403, "REASONER_MISMATCH")
    run = stopped.value.run
    assert run.stopped == f"LedgerError: {refused}"
    assert (run.requests, run.lines, run.exceptions) == ([], [], [])
    assert (run.decided, run.undecided) == (0, COUNT)
    assert ledger.store.rows == {}  # nothing recorded, no exception submitted


async def test_a_run_stopped_part_way_keeps_what_the_ledger_answered(
    ledger, refusing_client, business_date
):
    rows = synthetic_day(COUNT, business_date=business_date, run_tag="run")
    client = refusing_client(fail_request=2)

    with pytest.raises(DayRunStopped) as stopped:
        await run_day(rows, client, chunk_size=CHUNK)

    run = stopped.value.run
    assert run.stopped == "LedgerError: 500 INTERNAL: the test's ledger failed"
    assert [request["lines"] for request in run.requests] == [CHUNK]
    assert len(run.lines) == CHUNK
    # The first request was recorded but for its provoked line; no exception
    # was submitted, the provoked row included.
    assert run.decided == len(run.recorded) == CHUNK - 1
    assert (run.exceptions, client.submitted) == ([], [])
    assert run.undecided == COUNT - run.decided
    assert len(ledger.store.rows) == run.decided
    assert run.summary()["stopped"] == run.stopped


async def test_a_provoked_line_rejected_for_another_reason_is_reported_not_submitted(
    refusing_client, business_date
):
    rows = synthetic_day(COUNT, business_date=business_date, run_tag="run")
    plan = plan_day(score_rows(rows), chunk_size=CHUNK)
    assert plan.provoked is not None
    client = refusing_client(reject_lines={plan.provoked: "SUBMISSION_INVALID"})

    run = await run_day(rows, client, chunk_size=CHUNK)

    [rejection] = run.rejected
    assert (rejection.result.event_id, rejection.result.code) == (
        plan.provoked,
        "SUBMISSION_INVALID",
    )
    assert client.submitted == [
        s.event_id for s in plan.exceptions if s.event_id != plan.provoked
    ]
    assert run.action_of(plan.provoked) is None
    assert (run.decided, run.undecided) == (COUNT - 1, 1)


class _RaisingGate:
    """A rules gate that raises: the ledger fails it closed."""

    async def evaluate(self, request):
        msg = "the test's rules gate failed"
        raise RuntimeError(msg)

    def close(self) -> None:
        """Nothing to release."""


async def test_an_exception_whose_rules_gate_raised_is_held_and_counted_as_failed(
    ledger, business_date
):
    rules = dataclasses.replace(ledger.gates["rules"], gate=_RaisingGate())
    faulty = dataclasses.replace(ledger, gates={**ledger.gates, "rules": rules})
    client = LocalClient(faulty)
    rows = synthetic_day(COUNT, business_date=business_date, run_tag="run")
    plan = plan_day(score_rows(rows), chunk_size=CHUNK)

    run = await run_day(rows, client, chunk_size=CHUNK)

    assert [e.event_id for e in run.exceptions] == [s.event_id for s in plan.exceptions]
    for exception in run.exceptions:
        record = (await client.get_bundle(exception.decision_id)).gate("rules")
        assert record is not None
        assert (record.status, record.output) == ("failed", None)
        assert exception.decision_action == DecisionAction.HOLD
        assert exception.failure == record.reason
        assert (exception.matched_rules, exception.default_applied) == ((), None)
    assert run.summary()["exceptions"]["failures"] == len(plan.exceptions)


async def test_without_the_rule_set_every_exception_holds_and_the_failures_count(
    tmp_path, business_date
):
    empty = tmp_path / "rulesets"
    empty.mkdir()
    gate = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(empty)))
    client = LocalClient(memory_framework([PLUGIN], [gate]))

    rows, plan, run = await _run(client, business_date)

    assert (run.decided, run.undecided) == (len(rows), 0)
    count = len(plan.exceptions)
    assert len(run.exceptions) == count
    for exception in run.exceptions:
        assert exception.decision_action == DecisionAction.HOLD
        assert exception.failure == "unknown_ruleset"
        assert exception.matched_rules == ()
        assert exception.default_applied is None
    record = (await client.get_bundle(run.exceptions[0].decision_id)).gate("rules")
    assert record is not None
    assert record.status == "ran"
    assert record.output is not None
    assert record.output.verdict is None
    assert record.output.extra["failure"] == "unknown_ruleset"
    summary = run.summary()["exceptions"]
    assert (summary["decided"], summary["failures"]) == (count, count)
    assert summary["actions"] == {"HOLD": count}
    assert (summary["default_applied"], summary["matched_rules"]) == (0, {})
    assert [f["failure"] for f in summary["failure_sample"]] == ["unknown_ruleset"] * (
        min(count, SAMPLE_SIZE)
    )
    # A recorded row never reaches the gate: recorded as its rule says.
    assert len(run.recorded) == COUNT - count


async def test_a_recorded_line_and_an_exception_replay_to_the_same_action(
    client, business_date
):
    _, _, run = await _run(client, business_date)
    recorded = run.recorded[0].result
    exception = next(e for e in run.exceptions if e.matched_rules)
    version = registration_version(MOCK_BATCH_REGISTRATION)

    for decision_id, action in (
        (recorded.decision_id, recorded.decision_action),
        (exception.decision_id, exception.decision_action),
    ):
        replay = await client.replay(decision_id)

        assert replay.matched
        assert replay.replayed_action == replay.original_action == action.value
        assert replay.registration_version == version


async def test_verify_cites_each_matched_rule_with_the_rule_set_escrowed_and_intact(
    client, business_date
):
    _, _, run = await _run(client, business_date)
    exception = next(e for e in run.exceptions if e.matched_rules)

    verification = await client.verify(exception.decision_id)

    assert [ref.locator for ref in verification.refs] == list(exception.matched_rules)
    assert {
        (ref.gate_id, ref.source_id, ref.escrow, ref.status)
        for ref in verification.refs
    } == {("rules", "mock-batch-v1@1", "present", "intact")}
    assert verification.summary.intact == len(verification.refs)
    assert verification.summary.escrow_missing == 0
    # A recorded line: no gate ran on it, so it relied on no evidence.
    recorded = await client.verify(run.recorded[0].result.decision_id)
    assert recorded.refs == []


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _recorded(line, n, action):
    result = BatchLineResult(
        line=line,
        status="recorded",
        decision_id=f"decision-{n}",
        event_id=f"row-x-{n:06d}",
        decision_action=action,
        bypass_rule_holds=True,
    )
    return LineOutcome(1, result)


def _rejected(request, line, n, code):
    result = BatchLineResult(
        line=line,
        status="rejected",
        event_id=f"row-x-{n:06d}",
        code=code,
        detail=f"{code} (the test's)",
    )
    return LineOutcome(request, result)


def _failed(n):
    return ExceptionOutcome(
        event_id=f"row-x-{n:06d}",
        decision_id=f"decision-{n}",
        decision_action=DecisionAction.HOLD,
        decision_level_rule=None,
        matched_rules=(),
        default_applied=None,
        failure="unknown_ruleset",
        provoked=False,
    )


def test_the_summary_caps_its_lists_and_always_shows_the_provoked_rejection():
    recorded = [
        _recorded(1, 1, DecisionAction.ALLOW),
        _recorded(2, 2, DecisionAction.BLOCK),
        _recorded(3, 3, DecisionAction.ALLOW),
    ]
    # Seven other rejections come first; the provoked one is sent last.
    others = [_rejected(1, n, n, "SUBMISSION_INVALID") for n in range(4, 11)]
    provoked = _rejected(2, 1, 11, BATCH_RECORD_ONLY)
    exceptions = [_failed(n) for n in range(12, 19)]
    refused = [
        Refusal(f"row-x-{n:06d}", 422, "SUBMISSION_INVALID", "refused")
        for n in range(19, 26)
    ]
    run = DayRun(
        rows=25,
        provoked=provoked.result.event_id,
        lines=[*recorded, *others, provoked],
        exceptions=exceptions,
        refused=refused,
    )

    summary = run.summary()

    batch = summary["batch"]
    assert (batch["lines"], batch["recorded"], batch["rejected"]) == (11, 3, 8)
    assert batch["rejected_codes"] == {"BATCH_RECORD_ONLY": 1, "SUBMISSION_INVALID": 7}
    assert batch["rejections"] == [
        {**provoked.as_dict(), "provoked": True},
        *({**o.as_dict(), "provoked": False} for o in others[: SAMPLE_SIZE - 1]),
    ]
    assert batch["actions"] == {"ALLOW": 2, "BLOCK": 1}
    # The first recorded line of each action, by action.
    assert batch["recorded_sample"] == [recorded[0].as_dict(), recorded[1].as_dict()]
    handled = summary["exceptions"]
    assert (handled["decided"], handled["failures"], handled["refused"]) == (7, 7, 7)
    assert handled["sample"] == [e.as_dict() for e in exceptions[:SAMPLE_SIZE]]
    assert handled["failure_sample"] == handled["sample"]
    assert handled["refused_sample"] == [r.as_dict() for r in refused[:SAMPLE_SIZE]]
    assert (summary["rows"], summary["decided"], summary["undecided"]) == (25, 10, 15)
    assert (summary["stopped"], batch["cited_rule_not_held"]) == (None, 0)
    assert json.loads(json.dumps(summary)) == summary


def test_the_summary_marks_no_rejection_provoked_when_nothing_was_provoked():
    # Lines the ledger could not parse are rejected without an event id.
    unparsed = [
        LineOutcome(
            1,
            BatchLineResult(
                line=n,
                status="rejected",
                code="SUBMISSION_INVALID",
                detail="the body is not a valid Submission",
            ),
        )
        for n in range(1, 9)
    ]
    run = DayRun(rows=8, provoked=None, lines=unparsed)

    rejections = run.summary()["batch"]["rejections"]

    assert len(rejections) == SAMPLE_SIZE
    assert not any(r["provoked"] for r in rejections)


def test_the_summary_counts_recorded_lines_whose_cited_rule_did_not_hold():
    floored = LineOutcome(
        1,
        BatchLineResult(
            line=2,
            status="recorded",
            decision_id="decision-2",
            event_id="row-x-000002",
            decision_action=DecisionAction.HOLD,
            bypass_rule_holds=False,
        ),
    )
    run = DayRun(rows=2, lines=[_recorded(1, 1, DecisionAction.ALLOW), floored])

    batch = run.summary()["batch"]

    assert (batch["recorded"], batch["cited_rule_not_held"]) == (2, 1)


# ---------------------------------------------------------------------------
# The registration the ledger serves
# ---------------------------------------------------------------------------


async def test_a_ledger_serving_this_registration_is_no_problem(client):
    assert await registration_problem(client) is None


async def test_a_ledger_serving_another_version_of_the_registration_is_a_problem(
    tmp_path,
):
    older = MOCK_BATCH_REGISTRATION.model_copy(
        update={"reasoner_name": "Mock Batch Reasoner, an older copy"}
    )
    path = tmp_path / "registration.yaml"
    path.write_text(registration_yaml(older), encoding="utf-8")
    ledger = memory_framework((), [rules_gate_plugin()], registration_file=path)

    problem = await registration_problem(LocalClient(ledger))

    assert problem is not None
    assert registration_version(older) in problem
    assert registration_version(MOCK_BATCH_REGISTRATION) in problem
    assert f"file:{path}" in problem


async def test_a_ledger_that_does_not_register_the_mock_answers_not_found():
    ledger = memory_framework((), [rules_gate_plugin()])

    with pytest.raises(LedgerError) as missing:
        await registration_problem(LocalClient(ledger))

    assert (missing.value.status, missing.value.code) == (404, "REASONER_NOT_FOUND")


async def test_records_lists_every_line_then_every_exception_then_every_refusal(
    refusing_client, business_date
):
    rows = synthetic_day(COUNT, business_date=business_date, run_tag="run")
    plan = plan_day(score_rows(rows), chunk_size=CHUNK)
    client = refusing_client(plan.exceptions[1].event_id)
    run = await run_day(rows, client, chunk_size=CHUNK)

    records = run.records()

    assert [r["kind"] for r in records] == [
        *(["line"] * len(run.lines)),
        *(["exception"] * len(run.exceptions)),
        "refused",
    ]
    assert [
        (r["request"], r["line"], r["event_id"]) for r in records[: len(run.lines)]
    ] == [(o.request, o.result.line, o.result.event_id) for o in run.lines]
    exceptions = [r for r in records if r["kind"] == "exception"]
    assert [r["event_id"] for r in exceptions] == [e.event_id for e in run.exceptions]
    assert records[-1] == {"kind": "refused", **run.refused[0].as_dict()}
    assert json.loads(json.dumps(records)) == records
