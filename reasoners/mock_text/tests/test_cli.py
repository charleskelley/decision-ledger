"""The text mock's command line: one message decided, and its registration file.

- ``registration-yaml`` prints the registration file a ledger reads: the
  one the member ships;
- ``decide --dry-run`` prints the gate-mode ``Submission`` it would send and
  needs no settings: the message's id, customer, and channel are the
  arguments' (a random id and ``cust-demo`` on chat by default);
- ``decide`` refuses invalid settings (a blank or missing
  ``MOCK_TEXT_LEDGER_URL``, one that is not HTTP or that httpx cannot parse,
  a ``MOCK_TEXT_LEDGER_KEY`` an HTTP header cannot carry) with exit 2 before
  it reaches a ledger, a message the mock cannot take with exit 2, and the
  parser refuses an unknown ``--channel`` (exit 2);
- ``decide`` against a ledger prints the JSON report of what it decided
  (exit 0), and stops with exit 1 when the ledger refuses the message or
  cannot be reached, or when the decision's bundle cannot be read back (the
  decision is named on stderr); it closes the client every time.

``main`` runs the decision with ``asyncio.run``, so the tests that call it
are plain functions; the in-memory ledger stands in for the HTTP client
(``ledger_client``).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from app.client import LocalClient
from core.actions import DecisionAction
from core.exceptions import LedgerError
from core.identity import NativeSubject
from core.submission import Submission, validate_submission
from mock_text_reasoner.__main__ import DEFAULT_CUSTOMER_ID, main
from mock_text_reasoner.messages import MAX_TEXT_LENGTH
from mock_text_reasoner.pipeline import decision_report
from mock_text_reasoner.registry import (
    MOCK_TEXT_CUSTOMER_NAMESPACE,
    MOCK_TEXT_REGISTRATION,
    registration_yaml,
)

if TYPE_CHECKING:
    from pathlib import Path

    from app.api.models import DecisionReceipt
    from app.client import LedgerClient
    from app.plugin import FrameworkServices
    from core.bundle import DecisionBundle

#: The ledger URL the tests' settings name (no ledger listens there).
URL = "http://ledger.test"
#: Where ``main`` looks up the client it submits through.
LEDGER_CLIENT = "mock_text_reasoner.__main__.ledger_client"
#: The planted text: a hardship message, which proposes ALLOW.
HARDSHIP = "I lost my job and can't pay this month's installment."


class _ClientDouble:
    """A ledger client double that records ``aclose``.

    ``submit`` raises ``error`` and ``get_bundle`` raises ``read_error`` when
    one is given; otherwise every call passes on to ``inner``.
    """

    def __init__(
        self,
        inner: LedgerClient | None = None,
        *,
        error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.inner = inner
        self.error = error
        self.read_error = read_error
        self.submitted: list[str] = []
        self.closed = 0

    async def submit(self, submission: Submission) -> DecisionReceipt:
        self.submitted.append(submission.event_id)
        if self.error is not None:
            raise self.error
        assert self.inner is not None, "the double has no ledger behind it"
        return await self.inner.submit(submission)

    async def get_bundle(self, decision_id: str) -> DecisionBundle:
        if self.read_error is not None:
            raise self.read_error
        assert self.inner is not None, "the double has no ledger behind it"
        return await self.inner.get_bundle(decision_id)

    async def aclose(self) -> None:
        self.closed += 1
        if self.inner is not None:
            await self.inner.aclose()


def _no_client(settings: Any) -> Any:
    pytest.fail("decide built a ledger client it should not need")


def _report(out: str) -> dict[str, Any]:
    """The decision's JSON report: the indented object that ends the output.

    The ledger's own log lines may come before it on stdout.
    """
    lines = out.splitlines()
    return json.loads("\n".join(lines[lines.index("{") :]))


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    url: str | None = URL,
    key: str | None = None,
) -> None:
    """The mock's settings for one test: the environment only, no ``.env``."""
    monkeypatch.chdir(tmp_path)  # no .env is read
    for name, value in (("MOCK_TEXT_LEDGER_URL", url), ("MOCK_TEXT_LEDGER_KEY", key)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_registration_yaml_prints_the_registration_file_the_member_ships(
    capsys: pytest.CaptureFixture[str], registration_file: Path
):
    assert main(["registration-yaml"]) == 0

    printed = capsys.readouterr().out
    assert printed == registration_yaml()
    assert printed == registration_file.read_text(encoding="utf-8")


def test_a_dry_run_prints_the_gate_submission_and_needs_no_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _settings(monkeypatch, tmp_path, url="")  # a decide would refuse to run
    monkeypatch.setattr(LEDGER_CLIENT, _no_client)

    argv = ["decide", "--text", HARDSHIP, "--event-id", "msg-cli-dry"]
    argv += ["--customer-id", "cust-cli", "--channel", "email", "--dry-run"]
    assert main(argv) == 0

    captured = capsys.readouterr()
    submission = Submission.model_validate_json(captured.out)
    assert captured.err == ""
    assert (submission.mode, submission.proposed_action, submission.bypass) == (
        "gate",
        DecisionAction.ALLOW,
        None,
    )
    assert submission.reasoner_context.label_value == "hardship"
    assert submission.event_id == "msg-cli-dry"
    assert submission.native_subject == NativeSubject(
        native_namespace="mock-text-customer", native_subject_id="cust-cli"
    )
    assert (submission.raw_event["text"], submission.raw_event["channel"]) == (
        HARDSHIP,
        "email",
    )
    assert validate_submission(submission, MOCK_TEXT_REGISTRATION) is None


def test_a_dry_run_names_a_random_message_from_the_demo_customer_on_chat(
    capsys: pytest.CaptureFixture[str],
):
    assert main(["decide", "--text", "What fees do you charge?", "--dry-run"]) == 0
    first = Submission.model_validate_json(capsys.readouterr().out)
    assert main(["decide", "--text", "What fees do you charge?", "--dry-run"]) == 0
    second = Submission.model_validate_json(capsys.readouterr().out)

    assert re.fullmatch(r"msg-[0-9a-f]{12}", first.event_id)
    assert first.event_id != second.event_id
    assert first.native_subject is not None
    assert first.native_subject.native_subject_id == DEFAULT_CUSTOMER_ID == "cust-demo"
    assert first.raw_event["channel"] == "chat"
    assert first.proposed_action == DecisionAction.ALLOW


@pytest.mark.parametrize(
    ("url", "why"),
    [
        pytest.param(None, "MOCK_TEXT_LEDGER_URL is blank", id="unset"),
        pytest.param("", "MOCK_TEXT_LEDGER_URL is blank", id="blank"),
        pytest.param("   ", "MOCK_TEXT_LEDGER_URL is blank", id="spaces"),
        pytest.param("ledger:8000", "must be an http(s) URL", id="not-http"),
    ],
)
def test_decide_refuses_invalid_settings_with_exit_2_before_it_reaches_a_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    url: str | None,
    why: str,
):
    _settings(monkeypatch, tmp_path, url=url)
    monkeypatch.setattr(LEDGER_CLIENT, _no_client)

    assert main(["decide", "--text", HARDSHIP]) == 2

    captured = capsys.readouterr()
    assert "decide: the mock's settings are invalid" in captured.err
    assert why in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("two words", id="a-space"),
        pytest.param("line\nbreak", id="a-newline"),
        pytest.param("café-key", id="not-ascii"),
    ],
)
def test_decide_refuses_a_key_an_http_header_cannot_carry_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    key: str,
):
    _settings(monkeypatch, tmp_path, key=key)

    assert main(["decide", "--text", HARDSHIP]) == 2

    captured = capsys.readouterr()
    assert "decide: the mock's settings are invalid" in captured.err
    assert "visible ASCII" in captured.err
    assert key not in captured.err  # the key is never echoed
    assert captured.out == ""


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        pytest.param(["--text", ""], "at least 1 character", id="empty-text"),
        pytest.param(
            ["--text", " \n\t "], "the message text is blank", id="blank-text"
        ),
        pytest.param(
            ["--text", "x" * (MAX_TEXT_LENGTH + 1)],
            f"at most {MAX_TEXT_LENGTH} characters",
            id="text-too-long",
        ),
        pytest.param(
            ["--text", HARDSHIP, "--customer-id", ""],
            "at least 1 character",
            id="blank-customer",
        ),
    ],
)
def test_decide_refuses_a_message_the_mock_cannot_take_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    why: str,
):
    _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(LEDGER_CLIENT, _no_client)

    assert main(["decide", *argv]) == 2

    captured = capsys.readouterr()
    assert "decide: the message is invalid" in captured.err
    assert why in captured.err
    assert captured.out == ""


def test_an_unknown_channel_is_refused_by_the_parser_with_exit_2(
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as stopped:
        main(["decide", "--text", HARDSHIP, "--channel", "fax"])

    assert stopped.value.code == 2
    assert "invalid choice: 'fax'" in capsys.readouterr().err


def test_decide_prints_what_the_ledger_decided_and_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    holding_ledger: FrameworkServices,
):
    _settings(monkeypatch, tmp_path, url=f"{URL}/")
    double = _ClientDouble(LocalClient(holding_ledger))
    urls: list[str] = []

    def ledger_client(settings: Any) -> _ClientDouble:
        urls.append(settings.ledger_url)
        return double

    monkeypatch.setattr(LEDGER_CLIENT, ledger_client)

    assert main(["decide", "--text", HARDSHIP]) == 0

    assert urls == [URL]  # read from the settings, the trailing slash dropped
    report = _report(capsys.readouterr().out)
    assert (
        report["label"],
        report["proposed_action"],
        report["decision_action"],
        report["policy"]["protocol_hold"],
    ) == ("hardship", "ALLOW", "HOLD", "conflict_detected")
    [bundle] = holding_ledger.store.bundles()
    assert report == decision_report(bundle)
    assert double.closed == 1


def test_the_event_id_customer_and_channel_reach_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    holding_ledger: FrameworkServices,
):
    _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: LocalClient(holding_ledger))

    argv = ["decide", "--text", HARDSHIP, "--event-id", "msg-cli-0001"]
    argv += ["--customer-id", "cust-cli-7", "--channel", "app"]
    assert main(argv) == 0

    report = _report(capsys.readouterr().out)
    [bundle] = holding_ledger.store.bundles()
    assert report["event_id"] == bundle.submission.event_id == "msg-cli-0001"
    assert bundle.submission.native_subject == NativeSubject(
        native_namespace="mock-text-customer", native_subject_id="cust-cli-7"
    )
    assert bundle.entity_id == MOCK_TEXT_CUSTOMER_NAMESPACE.entity_id_of("cust-cli-7")
    assert bundle.submission.raw_event["channel"] == "app"


@pytest.mark.parametrize(
    ("error", "why"),
    [
        pytest.param(
            LedgerError(422, "SUBMISSION_INVALID", "the test's ledger refuses it"),
            "decide stopped: LedgerError: 422 SUBMISSION_INVALID: "
            "the test's ledger refuses it",
            id="refused",
        ),
        pytest.param(
            httpx.ConnectError("connection refused"),
            "decide stopped: ConnectError: connection refused",
            id="unreachable",
        ),
        pytest.param(
            ConnectionResetError("connection reset"),
            "decide stopped: ConnectionResetError: connection reset",
            id="connection-reset",
        ),
    ],
)
def test_a_ledger_that_does_not_decide_the_message_stops_decide_with_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    why: str,
):
    _settings(monkeypatch, tmp_path)
    double = _ClientDouble(error=error)
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: double)

    assert main(["decide", "--text", HARDSHIP, "--event-id", "msg-cli-0002"]) == 1

    captured = capsys.readouterr()
    assert why in captured.err
    assert captured.out == ""  # no report: nothing was decided
    assert double.submitted == ["msg-cli-0002"]
    assert double.closed == 1


def test_a_ledger_url_httpx_cannot_parse_is_refused_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _settings(monkeypatch, tmp_path, url="http://ledger:port")

    assert main(["decide", "--text", HARDSHIP]) == 2

    captured = capsys.readouterr()
    assert "decide: the mock's settings are invalid" in captured.err
    assert "Invalid port" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    "read_error",
    [
        pytest.param(LedgerError(503, "UNAVAILABLE", "try again"), id="refused"),
        pytest.param(httpx.ReadTimeout("timed out"), id="timeout"),
        pytest.param(ValueError("not a bundle"), id="unreadable"),
    ],
)
def test_a_decision_whose_bundle_cannot_be_read_back_is_named_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    holding_ledger: FrameworkServices,
    read_error: Exception,
):
    _settings(monkeypatch, tmp_path)
    double = _ClientDouble(LocalClient(holding_ledger), read_error=read_error)
    monkeypatch.setattr(LEDGER_CLIENT, lambda settings: double)

    assert main(["decide", "--text", HARDSHIP, "--event-id", "msg-cli-0003"]) == 1

    captured = capsys.readouterr()
    [bundle] = [
        b
        for b in holding_ledger.store.bundles()
        if b.submission.event_id == "msg-cli-0003"
    ]
    # The ledger decided it: the decision is named, though no report is printed.
    assert (
        f"decide: the ledger decided {bundle.decision_id} (HOLD), but its bundle "
        "could not be read back"
    ) in captured.err
    assert "{" not in captured.out.splitlines()  # the in-process ledger logs only
    assert double.closed == 1
