"""The batch mock's command line: a day scored and submitted, and its file.

- ``registration-yaml`` prints the registration file a ledger reads: the
  one the member ships;
- ``run --dry-run`` submits nothing: it prints the batch requests' NDJSON
  (every line a ``Submission``, as many as the plan sends, the provoked
  gate-mode line among them unless ``--no-provoke``), the same output for
  the same arguments, and a one-line summary on stderr;
- the parser refuses a ``--count`` below ``MIN_COUNT`` and a
  ``--chunk-size`` outside 1 to 10,000, the ledger's limit (exit 2);
- ``run`` refuses invalid settings (a blank or non-HTTP
  ``MOCK_BATCH_LEDGER_URL``, a ``MOCK_BATCH_LEDGER_KEY`` an HTTP header
  cannot carry) with exit 2 before it reaches a ledger;
- ``run`` against a ledger prints the JSON report of what it answered and
  writes every line's and exception's outcome to ``--results-out``: exit 0
  when it decided every row, 1 when it left one undecided, and 1 with ``run
  stopped`` when it refused or failed a batch request, the report and the
  file still holding what it answered until then;
- ``run`` against a ledger serving another version of the mock's
  registration sends nothing and exits 1.

``main`` runs the day with ``asyncio.run``, so the tests that call it are
plain functions; the in-memory ledger stands in for the HTTP client
(``ledger_client``).
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from app.auth import Principal
from app.client import LocalClient
from core.observation import registration_version
from core.submission import Submission
from mock_batch_reasoner.__main__ import main
from mock_batch_reasoner.batch import MAX_BATCH_LINES, plan_day, score_rows
from mock_batch_reasoner.eval.driver import rules_gate_plugin
from mock_batch_reasoner.registry import MOCK_BATCH_REGISTRATION, registration_yaml
from mock_batch_reasoner.synthetic import DEFAULT_SEED, MIN_COUNT, synthetic_day
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from pathlib import Path

#: The business date the tests' runs review, as the CLI takes it.
DATE = "2026-10-22"
#: Rows in the tests' runs: a few are exceptions.
COUNT = 300
#: Lines per batch request in the tests' runs: three requests.
CHUNK = 100
#: The ledger URL the tests' settings name (no ledger listens there).
URL = "http://ledger.test"
#: Where ``main`` looks up the client it submits through.
LEDGER_CLIENT = "mock_batch_reasoner.__main__.ledger_client"


def _run_args(*extra: str, tag: str = "cli") -> list[str]:
    return [
        "run",
        "--count",
        str(COUNT),
        "--run-tag",
        tag,
        "--date",
        DATE,
        "--chunk-size",
        str(CHUNK),
        *extra,
    ]


def _plan(tag: str = "cli", *, provoke: bool = True):
    rows = synthetic_day(COUNT, business_date=date.fromisoformat(DATE), run_tag=tag)
    return plan_day(score_rows(rows), chunk_size=CHUNK, provoke_record_only=provoke)


def _report(out: str) -> dict[str, Any]:
    """The run's JSON report: the indented object that ends the output.

    The ledger's own log lines may come before it on stdout.
    """
    lines = out.splitlines()
    return json.loads("\n".join(lines[lines.index("{") :]))


def test_registration_yaml_prints_the_registration_file_the_member_ships(
    capsys: pytest.CaptureFixture[str], registration_file: Path
):
    assert main(["registration-yaml"]) == 0

    printed = capsys.readouterr().out
    assert printed == registration_yaml()
    assert printed == registration_file.read_text(encoding="utf-8")


def test_a_dry_run_prints_the_batch_requests_as_ndjson_and_a_summary_on_stderr(
    capsys: pytest.CaptureFixture[str],
):
    assert main(_run_args("--dry-run")) == 0

    captured = capsys.readouterr()
    printed = [
        Submission.model_validate_json(line) for line in captured.out.splitlines()
    ]
    plan = _plan()
    assert printed == [submission for chunk in plan.chunks for submission in chunk]
    assert len(printed) == COUNT - len(plan.exceptions) + 1
    [gated] = [submission for submission in printed if submission.mode == "gate"]
    assert gated.event_id == plan.provoked == "row-cli-000005"
    sizes = ", ".join(str(len(chunk)) for chunk in plan.chunks)
    assert (
        f"dry run: {COUNT} rows; {len(printed)} batch lines in {len(plan.chunks)} "
        f"request(s) ({sizes}); {len(plan.exceptions)} exception(s) to submit on "
        f"their own; provoked: {plan.provoked}"
    ) in captured.err


def test_a_dry_run_without_provoking_prints_no_gate_mode_line(
    capsys: pytest.CaptureFixture[str],
):
    assert main(_run_args("--dry-run", "--no-provoke")) == 0

    captured = capsys.readouterr()
    printed = [
        Submission.model_validate_json(line) for line in captured.out.splitlines()
    ]
    plan = _plan(provoke=False)
    assert {submission.mode for submission in printed} == {"record"}
    assert len(printed) == COUNT - len(plan.exceptions)
    assert "provoked: None" in captured.err


def test_a_dry_run_prints_the_same_ndjson_for_the_same_arguments(
    capsys: pytest.CaptureFixture[str],
):
    assert main(_run_args("--dry-run")) == 0
    first = capsys.readouterr().out
    assert main(_run_args("--dry-run")) == 0
    second = capsys.readouterr().out
    assert main(_run_args("--dry-run", tag="other")) == 0
    other = capsys.readouterr().out

    assert first == second
    assert len(first.splitlines()) == COUNT - len(_plan().exceptions) + 1
    assert other != first


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        pytest.param(
            ["--count", str(MIN_COUNT - 1)],
            f"{MIN_COUNT - 1} is below {MIN_COUNT}",
            id="a-count-below-the-minimum",
        ),
        pytest.param(
            ["--chunk-size", str(MAX_BATCH_LINES + 1)],
            f"{MAX_BATCH_LINES + 1} is not from 1 to {MAX_BATCH_LINES}",
            id="a-chunk-over-the-ledgers-limit",
        ),
        pytest.param(
            ["--chunk-size", "0"],
            f"0 is not from 1 to {MAX_BATCH_LINES}",
            id="an-empty-chunk",
        ),
    ],
)
def test_the_parser_refuses_an_out_of_range_argument_with_exit_2(
    capsys: pytest.CaptureFixture[str], argv: list[str], why: str
):
    with pytest.raises(SystemExit) as stopped:
        main(["run", "--dry-run", *argv])

    assert stopped.value.code == 2
    assert why in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "why"),
    [
        pytest.param("", "MOCK_BATCH_LEDGER_URL is blank", id="blank"),
        pytest.param("ledger:8000", "must be an http(s) URL", id="not-http"),
    ],
)
def test_run_refuses_invalid_settings_with_exit_2_before_it_reaches_a_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    url: str,
    why: str,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_BATCH_LEDGER_URL", url)

    def ledger_client(settings: Any) -> Any:
        pytest.fail("run built a ledger client with invalid settings")

    monkeypatch.setattr(LEDGER_CLIENT, ledger_client)

    assert main(_run_args()) == 2

    captured = capsys.readouterr()
    assert "run: the mock's settings are invalid" in captured.err
    assert why in captured.err
    assert captured.out == ""


def test_run_refuses_a_key_an_http_header_cannot_carry_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_BATCH_LEDGER_URL", URL)
    monkeypatch.setenv("MOCK_BATCH_LEDGER_KEY", "two words")

    assert main(_run_args()) == 2

    captured = capsys.readouterr()
    assert "run: the mock's settings are invalid" in captured.err
    assert "two words" not in captured.err  # the key is never echoed
    assert captured.out == ""


def test_run_prints_what_the_ledger_answered_and_writes_every_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    client: LocalClient,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_BATCH_LEDGER_URL", f"{URL}/")
    urls: list[str] = []

    def ledger_client(settings: Any) -> LocalClient:
        urls.append(settings.ledger_url)
        return client

    monkeypatch.setattr(LEDGER_CLIENT, ledger_client)
    results = tmp_path / "results.ndjson"

    assert main(_run_args("--results-out", str(results))) == 0

    assert urls == [URL]  # read from the settings, the trailing slash dropped
    report = _report(capsys.readouterr().out)
    plan = _plan()
    assert (report["run_tag"], report["business_date"], report["seed"]) == (
        "cli",
        DATE,
        DEFAULT_SEED,
    )
    assert report["provoked"] == plan.provoked == "row-cli-000005"
    assert (report["rows"], report["decided"], report["undecided"]) == (
        COUNT,
        COUNT,
        0,
    )
    assert report["batch"]["rejected_codes"] == {"BATCH_RECORD_ONLY": 1}
    assert report["batch"]["requests"] == [
        {"lines": 100, "recorded": 99, "rejected": 1},
        {"lines": 100, "recorded": 100, "rejected": 0},
        {"lines": 97, "recorded": 97, "rejected": 0},
    ]
    assert report["exceptions"]["decided"] == len(plan.exceptions)
    records = [
        json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()
    ]
    lines = [r for r in records if r["kind"] == "line"]
    exceptions = [r for r in records if r["kind"] == "exception"]
    assert len(records) == len(lines) + len(exceptions)
    assert [r["event_id"] for r in lines] == [
        submission.event_id for chunk in plan.chunks for submission in chunk
    ]
    assert [r["event_id"] for r in lines if r["status"] == "rejected"] == [
        plan.provoked
    ]
    assert [r["event_id"] for r in exceptions] == [s.event_id for s in plan.exceptions]


def test_a_run_the_ledger_leaves_a_row_undecided_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusing_client: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_BATCH_LEDGER_URL", URL)
    refused = _plan().exceptions[1].event_id
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: refusing_client(refused))

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    report = _report(captured.out)
    assert (report["decided"], report["undecided"]) == (COUNT - 1, 1)
    assert [r["event_id"] for r in report["exceptions"]["refused_sample"]] == [refused]
    assert f"run: the ledger did not decide 1 of {COUNT} rows" in captured.err


def test_a_refused_batch_request_stops_the_run_with_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ledger: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_BATCH_LEDGER_URL", URL)
    # Another reasoner's key: the ledger refuses the whole request (403).
    other = LocalClient(ledger, principal=Principal.for_reasoner("other-reasoner"))
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: other)

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    assert "run stopped: LedgerError: 403 REASONER_MISMATCH" in captured.err
    assert "(run cli:" in captured.err
    report = _report(captured.out)
    assert report["stopped"].startswith("LedgerError: 403 REASONER_MISMATCH")
    assert (report["decided"], report["batch"]["requests"]) == (0, [])
    assert ledger.store.rows == {}


def test_a_run_stopped_part_way_prints_and_writes_what_the_ledger_answered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusing_client: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_BATCH_LEDGER_URL", URL)
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: refusing_client(fail_request=2))
    results = tmp_path / "results.ndjson"

    assert main(_run_args("--results-out", str(results))) == 1

    captured = capsys.readouterr()
    assert "run stopped: LedgerError: 500 INTERNAL" in captured.err
    report = _report(captured.out)
    assert report["run_tag"] == "cli"
    assert report["stopped"] == "LedgerError: 500 INTERNAL: the test's ledger failed"
    assert [r["lines"] for r in report["batch"]["requests"]] == [CHUNK]
    written = [json.loads(line) for line in results.read_text().splitlines()]
    assert [r["kind"] for r in written] == ["line"] * CHUNK


def test_a_ledger_serving_another_registration_stops_the_run_before_it_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_BATCH_LEDGER_URL", URL)
    older = MOCK_BATCH_REGISTRATION.model_copy(
        update={"reasoner_name": "Mock Batch Reasoner, an older copy"}
    )
    path = tmp_path / "registration.yaml"
    path.write_text(registration_yaml(older), encoding="utf-8")
    ledger = memory_framework((), [rules_gate_plugin()], registration_file=path)
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: LocalClient(ledger))

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    assert "run stopped before sending anything" in captured.err
    assert registration_version(older) in captured.err
    assert "{" not in captured.out.splitlines()  # no report: nothing was sent
    assert ledger.store.rows == {}
