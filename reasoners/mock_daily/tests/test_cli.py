"""The daily mock's command line: a day reconciled and submitted, and its file.

- ``registration-yaml`` prints the registration file a ledger reads: the
  one the member ships;
- ``run --dry-run`` submits nothing and needs no settings: it prints one
  ``Submission`` per row as NDJSON, in row order, and a one-line summary on
  stderr;
- the parser refuses a ``--count`` below ``MIN_COUNT`` (exit 2);
- ``run`` refuses invalid settings (a blank or non-HTTP
  ``MOCK_DAILY_LEDGER_URL``, a URL httpx cannot parse, a
  ``MOCK_DAILY_LEDGER_KEY`` an HTTP header cannot carry) and a
  ``--results-out`` that cannot be written with exit 2 before it reaches a
  ledger;
- ``run`` against a ledger prints the JSON report of what it answered and
  writes every decision's outcome to ``--results-out``: exit 0 when it
  decided every row and the rules gate gave a verdict on each, 1 when it
  left one undecided, 1 when the rules gate gave no verdict on a decision
  (the ledger held it), and 1 with ``run stopped`` when the ledger stopped it
  part way, the report and the file still holding what it answered until
  then, and stderr naming a row sent but not answered;
- ``run`` against a ledger serving another version of the mock's
  registration, or not registering the mock at all, sends nothing and
  exits 1.

``main`` runs the day with ``asyncio.run``, so the tests that call it are
plain functions; the in-memory ledger stands in for the HTTP client
(``ledger_client``).
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from app.client import LocalClient
from core.observation import registration_version
from core.submission import Submission
from mock_daily_reasoner.__main__ import main
from mock_daily_reasoner.daily import compute_day
from mock_daily_reasoner.eval.driver import rules_gate_plugin
from mock_daily_reasoner.plugin import PLUGIN
from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION, registration_yaml
from mock_daily_reasoner.synthetic import DEFAULT_SEED, MIN_COUNT, synthetic_day
from rules_gate import RulesGatePlugin
from rules_gate.settings import RulesGateSettings
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from pathlib import Path

#: The business date the tests' runs reconcile, as the CLI takes it.
DATE = "2026-10-22"
#: Rows in the tests' runs: a few are exceptions.
COUNT = 40
#: The ledger URL the tests' settings name (no ledger listens there).
URL = "http://ledger.test"
#: Where ``main`` looks up the client it submits through.
LEDGER_CLIENT = "mock_daily_reasoner.__main__.ledger_client"


def _run_args(*extra: str, tag: str = "cli") -> list[str]:
    return ["run", "--count", str(COUNT), "--run-tag", tag, "--date", DATE, *extra]


def _day(tag: str = "cli"):
    """The tests' day, computed and assembled as ``run`` does."""
    rows = synthetic_day(COUNT, business_date=date.fromisoformat(DATE), run_tag=tag)
    return compute_day(rows)


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


def test_a_dry_run_prints_every_submission_as_ndjson_and_needs_no_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", "")

    def ledger_client(settings: Any) -> Any:
        pytest.fail("a dry run built a ledger client")

    monkeypatch.setattr(LEDGER_CLIENT, ledger_client)

    assert main(_run_args("--dry-run")) == 0

    captured = capsys.readouterr()
    printed = [
        Submission.model_validate_json(line) for line in captured.out.splitlines()
    ]
    day = _day()
    assert printed == [c.submission for c in day]
    assert {submission.mode for submission in printed} == {"record", "gate"}
    exceptions = sum(c.is_exception for c in day)
    assert (
        f"dry run: {COUNT} rows; {COUNT - exceptions} recorded within tolerance, "
        f"{exceptions} exception(s) gated; one request per row"
    ) in captured.err


def test_the_parser_refuses_a_count_below_the_minimum_with_exit_2(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as stopped:
        main(["run", "--dry-run", "--count", str(MIN_COUNT - 1)])

    assert stopped.value.code == 2
    assert f"{MIN_COUNT - 1} is below {MIN_COUNT}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "why"),
    [
        pytest.param("", "MOCK_DAILY_LEDGER_URL is blank", id="blank"),
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
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", url)

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
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    monkeypatch.setenv("MOCK_DAILY_LEDGER_KEY", "two words")

    assert main(_run_args()) == 2

    captured = capsys.readouterr()
    assert "run: the mock's settings are invalid" in captured.err
    assert "two words" not in captured.err  # the key is never echoed
    assert captured.out == ""


def test_run_prints_what_the_ledger_answered_and_writes_every_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    client: LocalClient,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", f"{URL}/")
    urls: list[str] = []

    def ledger_client(settings: Any) -> LocalClient:
        urls.append(settings.ledger_url)
        return client

    monkeypatch.setattr(LEDGER_CLIENT, ledger_client)
    results = tmp_path / "results.ndjson"

    assert main(_run_args("--results-out", str(results))) == 0

    assert urls == [URL]  # read from the settings, the trailing slash dropped
    report = _report(capsys.readouterr().out)
    day = _day()
    exceptions = sum(c.is_exception for c in day)
    assert (report["run_tag"], report["business_date"], report["seed"]) == (
        "cli",
        DATE,
        DEFAULT_SEED,
    )
    assert (report["rows"], report["decided"], report["undecided"]) == (
        COUNT,
        COUNT,
        0,
    )
    assert (report["stopped"], report["refused"]) == (None, 0)
    assert report["recorded"]["decided"] == COUNT - exceptions
    assert report["recorded"]["actions"] == {"ALLOW": COUNT - exceptions}
    assert report["recorded"]["gate_status"] == {"ran": COUNT - exceptions}
    assert report["exceptions"]["decided"] == exceptions
    assert report["exceptions"]["gate_status"] == {"ran": exceptions}
    records = [
        json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["kind"] for r in records] == ["decision"] * COUNT
    assert [r["event_id"] for r in records] == [c.settlement.event_id for c in day]
    assert [r["mode"] for r in records] == [c.submission.mode for c in day]
    assert {r["gate_status"] for r in records} == {"ran"}


def test_a_run_whose_rules_gate_gave_no_verdict_exits_1_naming_the_cause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    empty = tmp_path / "rulesets"
    empty.mkdir()
    # A ledger whose rule-set directory does not hold the mock's rule set.
    unstaged = memory_framework(
        [PLUGIN], [RulesGatePlugin(RulesGateSettings(rulesets_dir=str(empty)))]
    )
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: LocalClient(unstaged))

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    report = _report(captured.out)
    assert (report["decided"], report["undecided"], report["stopped"]) == (
        COUNT,
        0,
        None,
    )
    assert report["recorded"]["failures"] + report["exceptions"]["failures"] == COUNT
    assert (
        f"run: the rules gate gave no verdict on {COUNT} of {COUNT} decisions, "
        "and the ledger held them"
    ) in captured.err
    assert "unknown_ruleset): stage the rule set (make prepare-rulesets)" in (
        captured.err
    )


def test_an_unwritable_results_file_is_refused_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusing_client: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    client = refusing_client()
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: client)
    missing = tmp_path / "no-such-directory" / "results.ndjson"

    assert main(_run_args("--results-out", str(missing))) == 2

    assert "run: --results-out cannot be written" in capsys.readouterr().err
    assert client.submitted == []


def test_run_refuses_a_ledger_url_httpx_cannot_parse_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", "http://[::1")

    assert main(_run_args()) == 2

    assert "run: the mock's settings are invalid" in capsys.readouterr().err


def test_a_run_stopped_with_a_row_in_doubt_names_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusing_client: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    unanswered = _day()[2].settlement.event_id
    monkeypatch.setattr(
        LEDGER_CLIENT, lambda settings: refusing_client(unanswered=[unanswered])
    )

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    assert _report(captured.out)["in_doubt"] == unanswered
    assert (
        f"row {unanswered} was sent and not answered, so the ledger may have "
        "decided it: look it up before sending it again"
    ) in captured.err


def test_a_run_the_ledger_leaves_a_row_undecided_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusing_client: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    refused = _day()[1].settlement.event_id
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: refusing_client(refused))

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    report = _report(captured.out)
    assert (report["decided"], report["undecided"], report["stopped"]) == (
        COUNT - 1,
        1,
        None,
    )
    assert [r["event_id"] for r in report["refused_sample"]] == [refused]
    assert f"run: the ledger did not decide 1 of {COUNT} rows" in captured.err


def test_a_run_stopped_part_way_prints_and_writes_what_the_ledger_answered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusing_client: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    day = _day()
    lost = day[5].settlement.event_id
    monkeypatch.setattr(
        LEDGER_CLIENT, lambda settings: refusing_client(lose_bundle=[lost])
    )
    results = tmp_path / "results.ndjson"

    assert main(_run_args("--results-out", str(results))) == 1

    captured = capsys.readouterr()
    assert "run stopped: LedgerError: 500 INTERNAL" in captured.err
    assert "(run cli:" in captured.err
    report = _report(captured.out)
    assert report["run_tag"] == "cli"
    assert report["stopped"] == (
        "LedgerError: 500 INTERNAL: the test's ledger lost the bundle"
    )
    # The row whose bundle was lost was decided: the report and the results
    # file keep it, from its receipt.
    assert (report["decided"], report["undecided"]) == (6, COUNT - 6)
    written = [
        json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["event_id"] for r in written] == [c.settlement.event_id for c in day[:6]]
    assert [r["bundle_read"] for r in written] == [True] * 5 + [False]


def test_a_ledger_serving_another_registration_stops_the_run_before_it_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stricter_ledger: Any,
    stricter_registration: Any,
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: LocalClient(stricter_ledger))

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    assert "run stopped before sending anything" in captured.err
    assert registration_version(stricter_registration) in captured.err
    assert registration_version(MOCK_DAILY_REGISTRATION) in captured.err
    assert "{" not in captured.out.splitlines()  # no report: nothing was sent
    assert stricter_ledger.store.rows == {}


def test_a_ledger_that_does_not_register_the_mock_stops_the_run_before_it_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_DAILY_LEDGER_URL", URL)
    ledger = memory_framework((), [rules_gate_plugin()])
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: LocalClient(ledger))

    assert main(_run_args()) == 1

    captured = capsys.readouterr()
    assert "run stopped: LedgerError: 404 REASONER_NOT_FOUND" in captured.err
    assert "{" not in captured.out.splitlines()  # no report: nothing was sent
    assert ledger.store.rows == {}
