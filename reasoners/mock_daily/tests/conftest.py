"""Fixtures for the daily mock's tests: no infrastructure.

``ledger`` is the framework composed in memory the way the bootstrap does
(``tests._memory_ledger.memory_framework``: the real registry, gate
admission, enforcement, store semantics, evidence escrow, and replay over
in-test storage), with the mock's plugin and the real rules gate over the
member's own rule set (``rules_gate_plugin``, as the eval driver composes
it), so the mock's day, its CLI, and its eval driver run through
``LocalClient`` (``client``: the operations the ledger's HTTP routes run)
without Postgres or a staged rule-set directory. ``settlement`` builds one
account's settlement for the day; ``refusing_client`` wraps ``client`` (or
another client) so that the ledger refuses the ``submit`` of chosen rows
(with a chosen status), loses the bundle of a chosen row it decided (with a
problem, or over the transport), decides a chosen row and never answers, or
cannot be reached from a chosen row on.
``stricter_registration`` is the mock's registration with a stricter
tolerance (its rule's version kept), and ``stricter_ledger`` a ledger that
serves it by file: a ledger serving another version of the mock's
registration. ``registration_file`` is the member's generated
``registration.yaml``.

Member tests have no ``__init__.py`` (pytest's importlib mode): shared
helpers are fixtures here or ``tests.*`` at the repository root.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from app.client import LocalClient
from core.exceptions import LedgerError
from core.gate import Compare
from mock_daily_reasoner.eval.driver import rules_gate_plugin
from mock_daily_reasoner.plugin import PLUGIN
from mock_daily_reasoner.registry import (
    MOCK_DAILY_REGISTRATION,
    WITHIN_TOLERANCE_RULE,
    registration_yaml,
)
from mock_daily_reasoner.settlements import AccountSettlement
from mock_daily_reasoner.synthetic import CUT_OFF
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from app.api.models import DecisionReceipt, RegisteredReasonerView
    from app.client import LedgerClient
    from app.plugin import FrameworkServices
    from core.bundle import DecisionBundle
    from core.observation import ReasonerRegistration
    from core.submission import Submission

#: The member's directory (``reasoners/mock_daily``).
MEMBER_DIR = Path(__file__).resolve().parent.parent
#: The generated registration file the member ships.
REGISTRATION_FILE = MEMBER_DIR / "registration.yaml"
#: The business day the tests' rows reconcile.
BUSINESS_DATE = date(2026, 10, 22)
#: When the tests' reconciliations are taken: the business day's cut-off (UTC).
AS_OF = datetime.combine(BUSINESS_DATE, CUT_OFF, tzinfo=UTC)
#: The stricter tolerance ``stricter_registration`` records under, in percent.
STRICTER_TOLERANCE_PCT = 0.5


class RefusingLedger:
    """A ledger client double: the ledger refuses or fails what the test chooses.

    Passes every call ``run_day`` and the CLI make on to ``inner`` (it has no
    batch endpoint: the daily mock never uses one), except:

    - a ``submit`` of a row in ``refuse``, which raises the ``LedgerError`` a
      ledger answers with (``status`` and ``code``), as ``HttpClient`` raises
      it from the problem document; the ledger decides nothing;
    - a ``get_bundle`` of the decision of a row in ``lose_bundle``, which
      fails after the ledger decided the row: with a 500, or with
      ``httpx.ReadTimeout`` when ``bundle_unreachable``;
    - a ``submit`` of a row in ``unanswered``, which the ledger decides and
      then never answers (``httpx.ReadTimeout``);
    - a ``submit`` of the row ``unreachable_from`` and of every row after it,
      which raises ``httpx.ConnectError`` before reaching the ledger.

    ``submitted`` lists every row ``submit`` was called with, and ``read``
    every decision whose bundle was read, in order.
    """

    def __init__(
        self,
        inner: LedgerClient,
        refuse: Iterable[str],
        *,
        status: int = 422,
        code: str = "SUBMISSION_INVALID",
        lose_bundle: Iterable[str] = (),
        bundle_unreachable: bool = False,
        unanswered: Iterable[str] = (),
        unreachable_from: str | None = None,
    ) -> None:
        self.inner = inner
        self.refuse = frozenset(refuse)
        self.status = status
        self.code = code
        self.lose_bundle = frozenset(lose_bundle)
        self.bundle_unreachable = bundle_unreachable
        self.unanswered = frozenset(unanswered)
        self.unreachable_from = unreachable_from
        self.unreachable = False
        self.submitted: list[str] = []
        self.read: list[str] = []
        self._event_of: dict[str, str] = {}

    async def submit(self, submission: Submission) -> DecisionReceipt:
        self.submitted.append(submission.event_id)
        if submission.event_id == self.unreachable_from:
            self.unreachable = True
        if self.unreachable:
            msg = "the test's ledger cannot be reached"
            raise httpx.ConnectError(msg)
        if submission.event_id in self.refuse:
            detail = f"the test's ledger refuses {submission.event_id!r}"
            raise LedgerError(self.status, self.code, detail)
        receipt = await self.inner.submit(submission)
        if submission.event_id in self.unanswered:
            msg = "the test's ledger decided the row and never answered"
            raise httpx.ReadTimeout(msg)
        self._event_of[receipt.decision_id] = submission.event_id
        return receipt

    async def get_bundle(self, decision_id: str) -> DecisionBundle:
        self.read.append(decision_id)
        if self._event_of.get(decision_id) in self.lose_bundle:
            if self.bundle_unreachable:
                msg = "the test's ledger stopped answering"
                raise httpx.ReadTimeout(msg)
            raise LedgerError(500, "INTERNAL", "the test's ledger lost the bundle")
        return await self.inner.get_bundle(decision_id)

    async def reasoner(self, reasoner_id: str) -> RegisteredReasonerView:
        return await self.inner.reasoner(reasoner_id)

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

    ``status`` and ``code`` (the refusal's), ``lose_bundle=[event_id, ...]``,
    ``bundle_unreachable``, ``unanswered=[event_id, ...]``, and
    ``unreachable_from=event_id`` pass through to ``RefusingLedger``;
    ``inner`` wraps another client instead of ``client``.
    """

    def make(
        *event_ids: str,
        status: int = 422,
        code: str = "SUBMISSION_INVALID",
        lose_bundle: Iterable[str] = (),
        bundle_unreachable: bool = False,
        unanswered: Iterable[str] = (),
        unreachable_from: str | None = None,
        inner: LedgerClient | None = None,
    ) -> RefusingLedger:
        return RefusingLedger(
            inner if inner is not None else client,
            event_ids,
            status=status,
            code=code,
            lose_bundle=lose_bundle,
            bundle_unreachable=bundle_unreachable,
            unanswered=unanswered,
            unreachable_from=unreachable_from,
        )

    return make


@pytest.fixture
def stricter_registration() -> ReasonerRegistration:
    """The mock's registration recording only below 0.5%, its rule still version 1.

    What a ledger serving another version of the mock's registration could
    hold: the mock cites ``within-tolerance`` version 1 as it registers it,
    and such a ledger finds the rule does not hold for a drift from 0.5% to
    1%.
    """
    rule = WITHIN_TOLERANCE_RULE.model_copy(
        update={
            "description": "abs_drift_pct below 0.5: record ALLOW",
            "predicate": Compare(
                field="feature_set.abs_drift_pct",
                op="lt",
                value=STRICTER_TOLERANCE_PCT,
            ),
        }
    )
    return MOCK_DAILY_REGISTRATION.model_copy(update={"bypass_rules": (rule,)})


@pytest.fixture
def stricter_ledger(
    tmp_path: Path, stricter_registration: ReasonerRegistration
) -> FrameworkServices:
    """A ledger serving ``stricter_registration`` by file, with the rules gate."""
    path = tmp_path / "registration.yaml"
    path.write_text(registration_yaml(stricter_registration), encoding="utf-8")
    return memory_framework((), [rules_gate_plugin()], registration_file=path)


@pytest.fixture
def member_dir() -> Path:
    """The member's directory (``reasoners/mock_daily``)."""
    return MEMBER_DIR


@pytest.fixture
def registration_file() -> Path:
    """The generated registration file the member ships (``registration.yaml``)."""
    return REGISTRATION_FILE


@pytest.fixture
def business_date() -> date:
    """The business day the tests' rows reconcile."""
    return BUSINESS_DATE


@pytest.fixture
def as_of() -> datetime:
    """When the tests' reconciliations are taken: the day's cut-off (UTC)."""
    return AS_OF


@pytest.fixture
def settlement() -> Callable[..., AccountSettlement]:
    """Build one settlement: ``settlement(n, target=..., actual=..., ...)``.

    An account that settled exactly its target of 1,000 by default;
    ``account`` defaults to one account per ``n``.
    """

    def make(
        n: int,
        *,
        target: float = 1_000.0,
        actual: float | None = None,
        account: str | None = None,
        as_of: datetime = AS_OF,
    ) -> AccountSettlement:
        return AccountSettlement(
            event_id=f"set-test-{n:04d}",
            as_of=as_of,
            account_id=account if account is not None else f"acct-test-{n:04d}",
            target_amount=target,
            actual_amount=actual if actual is not None else target,
        )

    return make
