"""Fixtures for the batch mock's tests: no infrastructure.

``ledger`` is the framework composed in memory the way the bootstrap does
(``tests._memory_ledger.memory_framework``: the real registry, gate
admission, enforcement, store semantics, evidence escrow, and replay over
in-test storage), with the mock's plugin and the real rules gate over the
member's own rule set (``rules_gate_plugin``, as the eval driver composes
it), so the mock's day, its CLI, and its eval driver run through
``LocalClient`` (``client``: the operations the ledger's HTTP routes run)
without Postgres or a staged rule-set directory. ``row`` builds one
account's end-of-day position; ``refusing_client`` wraps ``client`` so that
the ledger refuses the ``submit`` of chosen rows, rejects chosen batch lines
with another code, or fails a batch request; ``registration_file`` is the
member's generated ``registration.yaml``.

Member tests have no ``__init__.py`` (pytest's importlib mode): shared
helpers are fixtures here or ``tests.*`` at the repository root.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.client import LocalClient
from core.exceptions import LedgerError
from mock_batch_reasoner.eval.driver import rules_gate_plugin
from mock_batch_reasoner.plugin import PLUGIN
from mock_batch_reasoner.rows import AccountDay
from mock_batch_reasoner.synthetic import CUT_OFF
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from app.api.models import BatchResult, DecisionReceipt, RegisteredReasonerView
    from app.client import LedgerClient
    from app.plugin import FrameworkServices
    from core.bundle import DecisionBundle
    from core.submission import Submission

#: The member's directory (``reasoners/mock_batch``).
MEMBER_DIR = Path(__file__).resolve().parent.parent
#: The generated registration file the member ships.
REGISTRATION_FILE = MEMBER_DIR / "registration.yaml"
#: The business day the tests' rows review.
BUSINESS_DATE = date(2026, 10, 22)
#: When the tests' positions are taken: the business day's cut-off, in UTC.
AS_OF = datetime.combine(BUSINESS_DATE, CUT_OFF, tzinfo=UTC)


class RefusingLedger:
    """A ledger client double: the ledger refuses what the test chooses.

    Passes every call ``run_day`` and the CLI make on to ``inner``, except:

    - a ``submit`` of a row in ``refuse``, which raises the ``LedgerError`` a
      ledger answers with (``status`` and ``code``), as ``HttpClient`` raises
      it from the problem document;
    - a batch line the ledger rejected, whose row is in ``reject_lines``: its
      result carries that code instead (the ledger still recorded nothing);
    - the batch request numbered ``fail_request`` (from 1), and every one
      after it, which fail with a 500 before the ledger records anything.
    """

    status = 422
    code = "SUBMISSION_INVALID"

    def __init__(
        self,
        inner: LedgerClient,
        refuse: Iterable[str],
        *,
        reject_lines: Mapping[str, str] | None = None,
        fail_request: int | None = None,
    ) -> None:
        self.inner = inner
        self.refuse = frozenset(refuse)
        self.reject_lines = dict(reject_lines or {})
        self.fail_request = fail_request
        self.requests = 0
        self.submitted: list[str] = []

    async def submit(self, submission: Submission) -> DecisionReceipt:
        self.submitted.append(submission.event_id)
        if submission.event_id in self.refuse:
            detail = f"the test's ledger refuses {submission.event_id!r}"
            raise LedgerError(self.status, self.code, detail)
        return await self.inner.submit(submission)

    async def submit_batch(self, submissions: Iterable[Submission]) -> BatchResult:
        self.requests += 1
        if self.fail_request is not None and self.requests >= self.fail_request:
            raise LedgerError(500, "INTERNAL", "the test's ledger failed")
        result = await self.inner.submit_batch(submissions)
        results = [
            line.model_copy(update={"code": self.reject_lines[line.event_id]})
            if line.status == "rejected" and line.event_id in self.reject_lines
            else line
            for line in result.results
        ]
        return result.model_copy(update={"results": results})

    async def reasoner(self, reasoner_id: str) -> RegisteredReasonerView:
        return await self.inner.reasoner(reasoner_id)

    async def get_bundle(self, decision_id: str) -> DecisionBundle:
        return await self.inner.get_bundle(decision_id)

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.fixture
def ledger() -> FrameworkServices:
    """The ledger in memory: the mock by its plugin, the rules gate over its set."""
    return memory_framework([PLUGIN], [rules_gate_plugin()])


@pytest.fixture
def client(ledger: FrameworkServices) -> LocalClient:
    """The in-memory ledger as the trusted principal, through its operations."""
    return LocalClient(ledger)


@pytest.fixture
def refusing_client(client: LocalClient) -> Callable[..., RefusingLedger]:
    """``client``, refusing the ``submit`` of the rows named: ``(event_id, ...)``.

    ``reject_lines={event_id: code}`` and ``fail_request=n`` pass through to
    ``RefusingLedger``.
    """

    def make(
        *event_ids: str,
        reject_lines: Mapping[str, str] | None = None,
        fail_request: int | None = None,
    ) -> RefusingLedger:
        return RefusingLedger(
            client, event_ids, reject_lines=reject_lines, fail_request=fail_request
        )

    return make


@pytest.fixture
def member_dir() -> Path:
    """The member's directory (``reasoners/mock_batch``)."""
    return MEMBER_DIR


@pytest.fixture
def registration_file() -> Path:
    """The generated registration file the member ships (``registration.yaml``)."""
    return REGISTRATION_FILE


@pytest.fixture
def business_date() -> date:
    """The business day the tests' rows review."""
    return BUSINESS_DATE


@pytest.fixture
def as_of() -> datetime:
    """When the tests' positions are taken: the business day's cut-off (UTC)."""
    return AS_OF


@pytest.fixture
def row() -> Callable[..., AccountDay]:
    """Build one position: ``row(n, balance=..., days_past_due=..., ...)``.

    A current account by default: 250 owed on a 1,000 limit, nothing past
    due, no payment returned, not flagged; ``account`` defaults to one
    account per ``n``.
    """

    def make(
        n: int,
        *,
        balance: float = 250.0,
        credit_limit: float = 1000.0,
        days_past_due: int = 0,
        returned_payments: int = 0,
        needs_review: bool = False,
        account: str | None = None,
    ) -> AccountDay:
        return AccountDay(
            event_id=f"row-test-{n:04d}",
            as_of=AS_OF,
            account_id=account if account is not None else f"acct-test-{n:04d}",
            balance=balance,
            credit_limit=credit_limit,
            days_past_due=days_past_due,
            returned_payments=returned_payments,
            needs_review=needs_review,
        )

    return make
