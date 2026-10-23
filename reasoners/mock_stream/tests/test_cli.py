"""The stream mock's command line: its service, a replayed stream, its file.

- ``registration-yaml`` prints the registration file a ledger reads: the
  one the member ships;
- ``replay-events --dry-run`` prints the synthetic stream as NDJSON, every
  event id under the run tag, the same stream for the same arguments, or an
  NDJSON file back; a line of the file that is not JSON stops it (exit 1,
  why on stderr);
- a ``--start`` without a time zone and a ``--count`` below one are refused
  by the parser (exit 2);
- ``serve`` refuses invalid settings (a blank or non-HTTP
  ``MOCK_STREAM_LEDGER_URL``) with exit 2 before uvicorn starts, and runs
  the service's factory otherwise;
- ``summarize`` reports a replay's modes, actions, decision-level rules,
  gated decisions, and dead letters.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
import uvicorn

from mock_stream_reasoner.__main__ import main, summarize
from mock_stream_reasoner.registry import registration_yaml
from mock_stream_reasoner.replay import ReplaySummary, synthetic_stream

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mock_stream_reasoner.events import TransferEvent

START = "2026-10-01T09:00:00+00:00"


def _dry_run(count: int, *, run_tag: str = "abc123") -> list[str]:
    return [
        "replay-events",
        "--dry-run",
        "--count",
        str(count),
        "--run-tag",
        run_tag,
        "--start",
        START,
    ]


def test_registration_yaml_prints_the_registration_file_the_member_ships(
    capsys: pytest.CaptureFixture[str], registration_file: Path
):
    assert main(["registration-yaml"]) == 0

    printed = capsys.readouterr().out
    assert printed == registration_yaml()
    assert printed == registration_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("count", [60, 200])
def test_a_dry_run_prints_the_synthetic_stream_as_ndjson_under_the_run_tag(
    capsys: pytest.CaptureFixture[str], count: int
):
    assert main(_dry_run(count)) == 0

    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(payloads) == count
    assert all(p["event_id"].startswith("evt-abc123-") for p in payloads)
    assert payloads == synthetic_stream(
        count, run_tag="abc123", start=datetime(2026, 10, 1, 9, tzinfo=UTC)
    )


def test_a_dry_run_prints_the_same_stream_for_the_same_arguments(
    capsys: pytest.CaptureFixture[str],
):
    assert main(_dry_run(80)) == 0
    first = capsys.readouterr().out
    assert main(_dry_run(80)) == 0
    second = capsys.readouterr().out
    assert main(_dry_run(80, run_tag="xyz789")) == 0
    other = capsys.readouterr().out

    assert first == second
    assert len(first.splitlines()) == 80
    assert other != first


def test_a_start_without_a_time_zone_is_refused_with_exit_2(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as stopped:
        main(["replay-events", "--dry-run", "--start", "2026-10-01T09:00:00"])

    assert stopped.value.code == 2
    assert "has no time zone" in capsys.readouterr().err


def test_a_count_below_one_is_refused_with_exit_2(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as stopped:
        main(["replay-events", "--dry-run", "--count", "0"])

    assert stopped.value.code == 2
    assert "0 is not a positive integer" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "why"),
    [
        pytest.param("", "MOCK_STREAM_LEDGER_URL is blank", id="blank"),
        pytest.param("ledger:8000", "must be an http(s) URL", id="not-http"),
    ],
)
def test_serve_refuses_invalid_settings_before_uvicorn_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    url: str,
    why: str,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_STREAM_LEDGER_URL", url)

    def run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("serve started uvicorn with invalid settings")

    monkeypatch.setattr(uvicorn, "run", run)

    assert main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "serve: the mock's settings are invalid" in err
    assert why in err


def test_serve_runs_the_services_factory_under_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_STREAM_LEDGER_URL", "http://ledger.test")
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))

    assert main(["serve", "--host", "127.0.0.1", "--port", "8123"]) == 0

    assert calls == [
        (
            ("mock_stream_reasoner.app:create_app",),
            {"factory": True, "host": "127.0.0.1", "port": 8123},
        )
    ]


def test_a_dry_run_of_a_file_prints_its_events_back(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    transfer: Callable[..., TransferEvent],
):
    events = [
        transfer(1, "acct-a", 100.0).payload(),
        {"event_id": "evt-bad", "account_id": "acct-a", "amount": "lots"},
    ]
    stream = tmp_path / "stream.ndjson"
    stream.write_text(
        f"{json.dumps(events[0])}\n\n{json.dumps(events[1])}\n", encoding="utf-8"
    )

    assert main(["replay-events", "--dry-run", "--file", str(stream)]) == 0

    printed = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in printed] == events


def test_a_file_line_that_is_not_json_stops_the_replay_with_exit_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    transfer: Callable[..., TransferEvent],
):
    stream = tmp_path / "stream.ndjson"
    stream.write_text(
        f"{json.dumps(transfer(1, 'acct-a', 100.0).payload())}\nnot json\n",
        encoding="utf-8",
    )

    assert main(["replay-events", "--dry-run", "--file", str(stream)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "replay-events stopped: ValueError" in captured.err
    assert f"{stream}:2 is not JSON" in captured.err


def _decision(
    n: int,
    mode: str,
    action: str,
    *,
    rule: str | None = None,
    bypass: str | None = None,
    thin: bool = False,
) -> dict[str, Any]:
    return {
        "event_id": f"evt-run-{n:04d}",
        "account_id": f"acct-run-{n:02d}",
        "decision_id": f"d-{n}",
        "mode": mode,
        "decision_action": action,
        "bypass_rule_id": bypass,
        "decision_level_rule": rule,
        "thin_history": thin,
    }


def test_summarize_reports_modes_actions_rules_the_gated_and_the_dead_letters():
    decisions = [
        _decision(0, "record", "ALLOW", bypass="steady-activity", thin=True),
        _decision(1, "record", "ALLOW", bypass="steady-activity"),
        _decision(2, "gate", "HOLD", rule="tier3_novel_entity", thin=True),
        _decision(3, "gate", "CHALLENGE"),
        _decision(4, "record", "BLOCK", bypass="activity-surge"),
        _decision(5, "record", "ALLOW", bypass="steady-activity"),
    ]
    late = {
        "reason": "late",
        "event_id": "evt-run-late",
        "detail": "event time is 1:00:00 behind the watermark",
        "payload": {"event_id": "evt-run-late"},
    }
    malformed = {
        "reason": "malformed",
        "event_id": "evt-run-malformed",
        "detail": "timestamp: Field required",
        "payload": {"event_id": "evt-run-malformed"},
    }
    outcomes = {"accepted": 6, "dead_lettered:late": 1, "dead_lettered:malformed": 1}
    summary = ReplaySummary(
        published=8,
        outcomes=outcomes,
        decisions=decisions,
        dead_letters=[malformed, late],
    )
    payloads: list[dict[str, Any]] = [{"event_id": f"p-{n}"} for n in range(8)]

    report = summarize("run", summary, payloads)

    assert report == {
        "run_tag": "run",
        "published": 8,
        "outcomes": outcomes,
        "decided": 6,
        "decisions_missing": 0,
        "modes": {"gate": 2, "record": 4},
        "actions": {"ALLOW": 3, "BLOCK": 1, "CHALLENGE": 1, "HOLD": 1},
        "decision_level_rules": {"tier3_novel_entity": 1},
        "gated": [decisions[2], decisions[3]],
        "recorded_sample": [decisions[0], decisions[1], decisions[4]],
        "dead_letters": [
            {key: letter[key] for key in ("reason", "event_id", "detail")}
            for letter in (malformed, late)
        ],
    }
    assert json.loads(json.dumps(report)) == report
