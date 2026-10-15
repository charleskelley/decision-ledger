"""Behavioral tests for ``POST /api/v1/decisions:batch`` (NDJSON, record-only).

- Every recorded line is decided like a single record — its mandatory gates
  included — and the bundles are written in one ``write_many`` round trip.
- A line that does not parse, asks for a gate, or breaks its registration is
  rejected on its own, with its problem code; the other lines are recorded.
- A line naming a reasoner the key may not write for refuses the whole batch
  (403), and nothing is written.
- An empty batch is 422 ``BATCH_EMPTY``; an oversized one 413
  ``BATCH_TOO_LARGE``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import app.api.decisions as decisions_module
from core.actions import DecisionAction
from core.observation import GateBinding
from core.submission import BypassCitation
from tests._fake_gate.plugin import FakeGatePlugin, FakeLookupGatePlugin
from tests._memory_ledger import OPERATOR_KEY, OTHER_KEY, memory_framework
from tests._stub_reasoner.registry import STUB_REGISTRATION
from tests.app.conftest import start_ledger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.submission import Submission
    from tests.app.conftest import Ledger

_BATCH = "/api/v1/decisions:batch"


def _record(submission: Submission, n: int, score: float = 0.1) -> Submission:
    context = submission.reasoner_context.model_copy(update={"label_value": score})
    return submission.model_copy(
        update={"event_id": f"evt-batch-{n}", "reasoner_context": context}
    )


def _ndjson(*lines: Submission | str) -> str:
    return "".join(
        f"{line if isinstance(line, str) else line.model_dump_json()}\n"
        for line in lines
    )


def test_every_recorded_line_is_decided_and_written_in_one_round_trip(
    ledger: Ledger, submission
):
    lines = [_record(submission, n) for n in range(5)]
    high = _record(submission, 5, score=0.95).model_copy(
        update={
            "proposed_action": DecisionAction.BLOCK,
            "bypass": BypassCitation(rule_id="high-score"),
        }
    )

    response = ledger.post(_BATCH, _ndjson(*lines, high))

    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["recorded"], result["rejected"]) == (6, 0)
    assert [r["line"] for r in result["results"]] == [1, 2, 3, 4, 5, 6]
    assert [r["decision_action"] for r in result["results"]] == ["ALLOW"] * 5 + [
        "BLOCK"
    ]
    assert all(r["bypass_rule_holds"] is True for r in result["results"])
    assert ledger.store.write_many_calls == [6]
    stored = {b.decision_id: b for b in ledger.store.bundles()}
    assert {r["decision_id"] for r in result["results"]} == set(stored)
    assert {b.submission.event_id for b in stored.values()} == {
        f"evt-batch-{n}" for n in range(6)
    }


def test_bad_lines_are_rejected_on_their_own(
    ledger: Ledger, submission, gated_submission
):
    unregistered = _record(submission, 3).model_copy(
        update={"bypass": BypassCitation(rule_id="nope")}
    )
    body = _ndjson(
        _record(submission, 1),
        "{not json",
        gated_submission,
        unregistered,
        "",
        _record(submission, 2),
    )

    result = ledger.post(_BATCH, body).json()

    assert (result["recorded"], result["rejected"]) == (2, 3)
    by_line = {r["line"]: r for r in result["results"]}
    assert sorted(by_line) == [1, 2, 3, 4, 6]  # the blank line 5 is skipped
    assert by_line[1]["status"] == by_line[6]["status"] == "recorded"
    assert (by_line[2]["status"], by_line[2]["code"]) == (
        "rejected",
        "SUBMISSION_INVALID",
    )
    assert by_line[3]["code"] == "BATCH_RECORD_ONLY"
    assert by_line[3]["event_id"] == gated_submission.event_id
    assert by_line[4]["code"] == "BYPASS_RULE_UNREGISTERED"
    assert len(ledger.store.bundles()) == 2


def test_a_line_for_another_reasoner_refuses_the_whole_batch(
    ledger: Ledger, submission
):
    body = _ndjson(_record(submission, 1), _record(submission, 2))
    response = ledger.post(_BATCH, body, key=OTHER_KEY)
    assert response.status_code == 403
    assert response.json()["code"] == "REASONER_MISMATCH"
    assert ledger.store.rows == {}
    response = ledger.post(_BATCH, body, key=OPERATOR_KEY)
    assert response.json()["code"] == "REASONER_KEY_REQUIRED"


def test_an_empty_batch_is_422(ledger: Ledger):
    response = ledger.post(_BATCH, "\n\n")
    assert response.status_code == 422
    assert response.json()["code"] == "BATCH_EMPTY"


def test_an_oversized_batch_is_413(
    ledger: Ledger, submission, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(decisions_module, "MAX_BATCH_LINES", 2)
    body = _ndjson(*(_record(submission, n) for n in range(3)))
    response = ledger.post(_BATCH, body)
    assert response.status_code == 413
    assert response.json()["code"] == "BATCH_TOO_LARGE"
    assert ledger.store.rows == {}


@pytest.fixture
def ledger_with_mandatory_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Ledger]:
    """The stub binds a mandatory lookup gate next to its advisory fake gate."""
    from tests._stub_reasoner.plugin import PLUGIN

    registration = STUB_REGISTRATION.model_copy(
        update={
            "gates": (
                GateBinding(gate_id="fake-lookup", mandatory=True),
                GateBinding(gate_id="fake"),
            )
        }
    )

    class _Plugin:
        prompts_dir = PLUGIN.prompts_dir
        corpus_dir = PLUGIN.corpus_dir

        def __init__(self) -> None:
            self.registration = registration

    lookup = FakeLookupGatePlugin()
    fake = FakeGatePlugin()
    framework = memory_framework([_Plugin()], [lookup, fake])
    ledger_iter = start_ledger(monkeypatch, framework, fake)
    ledger = next(ledger_iter)
    ledger.lookup = lookup  # type: ignore[attr-defined]
    yield ledger
    next(ledger_iter, None)


def test_a_batch_runs_each_records_mandatory_gates(
    ledger_with_mandatory_lookup: Ledger, submission
):
    ledger = ledger_with_mandatory_lookup
    listed = _record(submission, 1)
    ledger.lookup.listed[listed.entity_id] = DecisionAction.BLOCK  # type: ignore[attr-defined]
    with_lookup = [
        line.model_copy(
            update={"gate_contexts": (*line.gate_contexts, _lookup_context())}
        )
        for line in (listed,)
    ]

    result = ledger.post(_BATCH, _ndjson(*with_lookup)).json()

    [line] = result["results"]
    assert line["status"] == "recorded"
    assert line["decision_action"] == "BLOCK"  # the mandatory gate raised it
    [bundle] = ledger.store.bundles()
    assert [(g.gate_id, g.status) for g in bundle.gates] == [
        ("fake-lookup", "ran"),
        ("fake", "skipped"),
    ]


def _lookup_context():
    from core.observation import GateContext

    return GateContext(gate_id="fake-lookup", gate_config={})


# ---------------------------------------------------------------------------
# NDJSON framing and body bounds
# ---------------------------------------------------------------------------


def _with_note(submission: Submission, n: int, note: str) -> Submission:
    line = _record(submission, n)
    return line.model_copy(update={"raw_event": {**line.raw_event, "note": note}})


def test_lines_end_at_line_feeds_only(ledger: Ledger, submission):
    # JSON strings may hold U+2028, U+2029, and U+0085 raw, and
    # model_dump_json writes them raw: they must not split a line.
    line_sep, para_sep, nel = chr(0x2028), chr(0x2029), chr(0x85)
    lines = [
        _with_note(submission, 1, f"pasted{line_sep}text"),
        _with_note(submission, 2, f"next{nel}line{para_sep}paragraph"),
        _record(submission, 3),
    ]
    body = _ndjson(*lines)
    assert line_sep in body
    assert nel in body

    result = ledger.post(_BATCH, body).json()

    assert (result["recorded"], result["rejected"]) == (3, 0)
    assert [r["line"] for r in result["results"]] == [1, 2, 3]
    stored = {b.submission.event_id: b for b in ledger.store.bundles()}
    assert stored["evt-batch-1"].submission.raw_event["note"] == f"pasted{line_sep}text"


def test_crlf_line_endings_are_accepted(ledger: Ledger, submission):
    body = _ndjson(_record(submission, 1), _record(submission, 2)).replace("\n", "\r\n")
    result = ledger.post(_BATCH, body).json()
    assert (result["recorded"], result["rejected"]) == (2, 0)


def test_a_line_that_is_not_utf8_is_rejected_on_its_own(ledger: Ledger, submission):
    good = _record(submission, 1).model_dump_json().encode()
    bad = _with_note(submission, 2, "cafe").model_dump_json().encode()
    bad = bad.replace(b'"cafe"', b'"caf\xe9"')  # Latin-1, not UTF-8

    result = ledger.post(_BATCH, good + b"\n" + bad + b"\n").json()

    by_line = {r["line"]: r for r in result["results"]}
    assert by_line[1]["status"] == "recorded"
    assert (by_line[2]["status"], by_line[2]["code"]) == (
        "rejected",
        "SUBMISSION_INVALID",
    )
    assert [b.submission.event_id for b in ledger.store.bundles()] == ["evt-batch-1"]


def test_an_oversized_batch_body_is_413_before_it_is_parsed(
    ledger: Ledger, submission, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(decisions_module, "MAX_BATCH_BYTES", 200)
    body = _ndjson(_record(submission, 1), _record(submission, 2))
    assert len(body.encode()) > 200

    response = ledger.post(_BATCH, body)

    assert response.status_code == 413
    assert response.json()["code"] == "BODY_TOO_LARGE"
    assert response.json()["limit_bytes"] == 200
    assert ledger.store.rows == {}


async def test_the_operation_bounds_lines_before_building_them(
    ledger: Ledger, submission, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(decisions_module, "MAX_BATCH_LINES", 3)
    from app.auth import TRUSTED
    from core.exceptions import LedgerError

    body = _ndjson(*(_record(submission, n) for n in range(4))) + "\n" * 50
    with pytest.raises(LedgerError) as err:
        await decisions_module.record_batch(
            body, principal=TRUSTED, framework=ledger.framework
        )
    assert (err.value.status, err.value.code) == (413, "BATCH_TOO_LARGE")
