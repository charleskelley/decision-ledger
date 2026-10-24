"""Smoke: a synthetic day through the ledger, end to end, without Docker.

The batch mock's happy path in one test (``make test-smoke``): a synthetic
day scored, its records sent in batch requests and its exceptions submitted
one at a time to the ledger in memory (the mock's plugin and the real rules
gate over the member's rule set). Every row is decided, exactly one line is
rejected (the provoked ``BATCH_RECORD_ONLY``), the rules gate decides every
exception, and a recorded decision and an exception replay to the same
action.
"""

from __future__ import annotations

import pytest

from mock_batch_reasoner.batch import run_day
from mock_batch_reasoner.synthetic import synthetic_day


@pytest.mark.smoke
async def test_a_synthetic_day_is_decided_end_to_end_and_replays(client, business_date):
    rows = synthetic_day(300, business_date=business_date, run_tag="smoke")

    run = await run_day(rows, client, chunk_size=100)

    assert (run.decided, run.undecided) == (len(rows), 0)
    [rejection] = run.rejected
    assert rejection.result.code == "BATCH_RECORD_ONLY"
    assert rejection.result.event_id == run.provoked
    assert run.exceptions
    # The rules gate gave a verdict on each: no failure, a rule or the default.
    assert [e.failure for e in run.exceptions] == [None] * len(run.exceptions)
    assert all(e.default_applied is not None for e in run.exceptions)
    exception = next(e for e in run.exceptions if e.matched_rules)
    for decision_id in (run.recorded[0].result.decision_id, exception.decision_id):
        replay = await client.replay(decision_id)
        assert replay.matched
