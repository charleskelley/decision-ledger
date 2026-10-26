"""The daily mock's run — a day's settlements into the ledger, one at a time.

``run_day`` computes every row's drift and assembles its submission
(``assembler``), then submits each one on its own (``LedgerClient.submit``,
one ``POST /api/v1/decisions`` per row), in row order, and reads the
decision's bundle back (``LedgerClient.get_bundle``) for the rules gate's
verdict. A row within tolerance is recorded ALLOW; any other row is an
exception, gated with no proposal. The rules gate is bound mandatory, so it
runs on both: on a recorded row it matches no rule and permits the default,
so the record keeps its action; on an exception its rules decide by the size
of the drift. The batch endpoint is the batch mock's pattern: this mock
submits row by row, recorded rows included, so every decision is one
request whose answer names its gate.

A row the ledger refuses for itself (a 4xx problem about the submission)
is reported, and the run goes on. A refusal of the key (401 or 403: a key
may read the mock's registration and still not write its decisions), a
server error (5xx), a ledger that cannot be reached, or a failed read of a
decided row's bundle stops the run: ``run_day`` raises ``DayRunStopped``
with everything the ledger answered until then. The ledger does not
deduplicate, so the operator must know every row a stopped run may have
decided: a row decided before its bundle read failed is kept among the
decisions, from its receipt (``bundle_read`` false), and a row whose request
was sent but not answered (a transport error after sending, or a 5xx) is
named ``in_doubt``, to be looked up before it is sent again.

A cited bypass rule that does not hold is not a refusal: the ledger records
the row under a HOLD floor (``bypass_rule_holds: false``, decision-level
rule ``bypass_rule_not_satisfied``). The mock cites the rule only when it
holds under its own registration; a ledger serving another version of it
may disagree, which ``registration_problem`` checks before a run and the
run's report counts (``cited_rule_not_held``).

The ledger does not deduplicate: every row it decides is a new decision,
whatever its ``event_id``. A day's rows must have distinct ids, and a day
sent twice is decided twice.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from core.exceptions import LedgerError
from core.observation import registration_version
from mock_daily_reasoner.assembler import RULES_GATE_ID, build_submission
from mock_daily_reasoner.compute import compute_drift
from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from app.api.models import DecisionReceipt
    from app.client import LedgerClient
    from core.actions import DecisionAction
    from core.bundle import DecisionBundle
    from core.observation import ReasonerRegistration
    from core.submission import Submission
    from mock_daily_reasoner.compute import SettlementDrift
    from mock_daily_reasoner.settlements import AccountSettlement

#: How many decisions of each kind a run's report lists.
SAMPLE_SIZE = 5
#: Statuses that refuse the key, not the row: they stop the run.
_KEY_REFUSED = frozenset({401, 403})
#: Transport errors raised before a request reached the ledger.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


@dataclass(frozen=True)
class ComputedSettlement:
    """One row, its drift, and the submission assembled from them.

    Attributes:
        settlement: The account's settlement for the day.
        drift: Its computed drift.
        submission: Recorded when the account is within tolerance, gated
            otherwise.
    """

    settlement: AccountSettlement
    drift: SettlementDrift
    submission: Submission

    @property
    def is_exception(self) -> bool:
        """No bypass rule covers the row: the rules gate alone decides it."""
        return self.submission.mode == "gate"


def compute_day(settlements: Iterable[AccountSettlement]) -> list[ComputedSettlement]:
    """Compute every row's drift and assemble its submission, in row order.

    Args:
        settlements: The day's rows.

    Returns:
        One ``ComputedSettlement`` per row.

    Raises:
        ValueError: Two rows share an ``event_id``.
    """
    computed: list[ComputedSettlement] = []
    seen: set[str] = set()
    for settlement in settlements:
        if settlement.event_id in seen:
            msg = f"two rows share the event id {settlement.event_id!r}"
            raise ValueError(msg)
        seen.add(settlement.event_id)
        drift = compute_drift(settlement)
        computed.append(
            ComputedSettlement(settlement, drift, build_submission(settlement, drift))
        )
    return computed


@dataclass(frozen=True)
class DecisionOutcome:
    """One row the ledger decided, and how the rules gate ended on it.

    Attributes:
        event_id: The row's id.
        decision_id: The decision.
        mode: ``record`` (within tolerance) or ``gate`` (an exception).
        proposed_action: The record's own action (ALLOW); ``None`` gated.
        decision_action: The action the ledger decided.
        decision_level_rule: The decision-level rule that fired, if any.
        bypass_rule_holds: Whether the cited rule held (``None`` gated).
        gate_status: How the rules gate ended: ``ran``, ``failed``, or
            ``skipped`` (``missing`` when the bundle has no record of it).
        matched_rules: The rule-set rules that matched (the gate's verdict).
        default_applied: No rule matched and the rule set's default applied;
            ``None`` when the gate gave no verdict.
        failure: Why the rules gate gave no verdict (its ``extra.failure``,
            the reason a failed gate is recorded with, or ``"no verdict"``);
            ``None`` when it gave one, or when the bundle was not read and
            the receipt names no failure.
        bundle_read: Whether the decision's bundle was read back. ``False``
            only for the row a run stopped on after the ledger had decided
            it: its outcome is the receipt's (the gate's status, and the
            reason it failed, if it did), with no matched rules and no
            default known.
    """

    event_id: str
    decision_id: str
    mode: str
    proposed_action: DecisionAction | None
    decision_action: DecisionAction
    decision_level_rule: str | None
    bypass_rule_holds: bool | None
    gate_status: str
    matched_rules: tuple[str, ...]
    default_applied: bool | None
    failure: str | None
    bundle_read: bool = True

    @property
    def raised(self) -> bool:
        """A recorded row whose decided action is not the record's own."""
        return self.mode == "record" and self.decision_action != self.proposed_action

    def as_dict(self) -> dict[str, Any]:
        """The outcome as JSON-ready data."""
        return {
            "event_id": self.event_id,
            "decision_id": self.decision_id,
            "mode": self.mode,
            "proposed_action": (
                self.proposed_action.value if self.proposed_action is not None else None
            ),
            "decision_action": self.decision_action.value,
            "decision_level_rule": self.decision_level_rule,
            "bypass_rule_holds": self.bypass_rule_holds,
            "gate_status": self.gate_status,
            "matched_rules": list(self.matched_rules),
            "default_applied": self.default_applied,
            "failure": self.failure,
            "bundle_read": self.bundle_read,
        }


def decision_outcome(
    receipt: DecisionReceipt, bundle: DecisionBundle
) -> DecisionOutcome:
    """Read how the rules gate ended on one decision from its bundle.

    Args:
        receipt: What ``submit`` returned.
        bundle: The decision's bundle.

    Returns:
        The outcome.
    """
    record = bundle.gate(RULES_GATE_ID)
    output = record.output if record is not None else None
    snapshot = output.evidence_snapshot if output is not None else None
    if not isinstance(snapshot, dict):
        snapshot = {}
    matched = snapshot.get("matched_rule_ids")
    default = snapshot.get("default_applied")
    failure: Any = output.extra.get("failure") if output is not None else None
    if output is None:
        failure = record.reason if record is not None else "not in the gate set"
    elif output.verdict is None and failure is None:
        failure = "no verdict"
    has_verdict = output is not None and output.verdict is not None
    return DecisionOutcome(
        event_id=receipt.event_id,
        decision_id=receipt.decision_id,
        mode=receipt.mode,
        proposed_action=bundle.submission.proposed_action,
        decision_action=receipt.decision_action,
        decision_level_rule=receipt.decision_level_rule,
        bypass_rule_holds=receipt.bypass_rule_holds,
        gate_status=record.status if record is not None else "missing",
        matched_rules=tuple(str(rule) for rule in matched)
        if isinstance(matched, list)
        else (),
        default_applied=bool(default) if has_verdict else None,
        failure=str(failure) if failure is not None else None,
    )


def receipt_outcome(
    receipt: DecisionReceipt, submission: Submission
) -> DecisionOutcome:
    """What one decision's receipt says, for a row whose bundle was not read.

    Args:
        receipt: What ``submit`` returned.
        submission: The row's submission (its proposal).

    Returns:
        The outcome, with ``bundle_read`` false: the rules gate's status and
        any failure reason come from the receipt's gate summary, and its
        matched rules and default are unknown.
    """
    summary = next((g for g in receipt.gates if g.gate_id == RULES_GATE_ID), None)
    return DecisionOutcome(
        event_id=receipt.event_id,
        decision_id=receipt.decision_id,
        mode=receipt.mode,
        proposed_action=submission.proposed_action,
        decision_action=receipt.decision_action,
        decision_level_rule=receipt.decision_level_rule,
        bypass_rule_holds=receipt.bypass_rule_holds,
        gate_status=summary.status if summary is not None else "missing",
        matched_rules=(),
        default_applied=None,
        failure=(
            summary.reason
            if summary is not None and summary.status == "failed"
            else None
        ),
        bundle_read=False,
    )


@dataclass(frozen=True)
class Refusal:
    """A row the ledger refused (``submit`` answered with a problem).

    Attributes:
        event_id: The row's id.
        status: The HTTP status.
        code: The problem's code.
        detail: The problem's detail.
    """

    event_id: str
    status: int
    code: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        """The refusal as JSON-ready data."""
        return {
            "event_id": self.event_id,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass
class DayRun:
    """What the ledger answered for one day.

    Attributes:
        rows: Rows in the day.
        decisions: Every row the ledger decided, in row order.
        refused: Every row the ledger refused.
        stopped: Why the run stopped part way (the error), or ``None`` when
            it ran to the end.
        in_doubt: The ``event_id`` of the row the run stopped on when its
            request was sent but not answered (a transport error after
            sending, or a 5xx): the ledger may have decided it, so it must
            be looked up before it is sent again. ``None`` otherwise.
    """

    rows: int
    decisions: list[DecisionOutcome] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    stopped: str | None = None
    in_doubt: str | None = None

    @property
    def recorded(self) -> list[DecisionOutcome]:
        """The rows the ledger decided as records (within tolerance)."""
        return [d for d in self.decisions if d.mode == "record"]

    @property
    def exceptions(self) -> list[DecisionOutcome]:
        """The rows the ledger decided in gate mode (the exceptions)."""
        return [d for d in self.decisions if d.mode == "gate"]

    @property
    def decided(self) -> int:
        """Rows the ledger decided."""
        return len(self.decisions)

    @property
    def undecided(self) -> int:
        """Rows the run has no decision for: refused, never sent, or in doubt."""
        return self.rows - self.decided

    def action_of(self, event_id: str) -> DecisionAction | None:
        """The action the ledger decided for one row, if it decided it."""
        for decision in self.decisions:
            if decision.event_id == event_id:
                return decision.decision_action
        return None

    def summary(self) -> dict[str, Any]:
        """The run's report, JSON-ready: counts by action, rule, and gate status.

        Samples are capped at ``SAMPLE_SIZE`` entries; every decision and
        refusal is in ``records()``.
        """
        recorded = self.recorded
        exceptions = self.exceptions
        return {
            "rows": self.rows,
            "decided": self.decided,
            "undecided": self.undecided,
            "stopped": self.stopped,
            "in_doubt": self.in_doubt,
            "recorded": {
                **_kind_summary(recorded),
                "raised": sum(1 for d in recorded if d.raised),
                "cited_rule_not_held": sum(
                    1 for d in recorded if d.bypass_rule_holds is False
                ),
            },
            "exceptions": _kind_summary(exceptions),
            "refused": len(self.refused),
            "refused_sample": [r.as_dict() for r in self.refused[:SAMPLE_SIZE]],
        }

    def records(self) -> list[dict[str, Any]]:
        """Every decision and every refusal, JSON-ready, for a results file.

        Returns:
            One ``{"kind": "decision", ...}`` per decided row, in row order,
            then one ``{"kind": "refused", ...}`` per refused one.
        """
        return [
            *({"kind": "decision", **d.as_dict()} for d in self.decisions),
            *({"kind": "refused", **r.as_dict()} for r in self.refused),
        ]


def _kind_summary(decisions: Sequence[DecisionOutcome]) -> dict[str, Any]:
    """Counts of one kind of decision (recorded or exceptions), with samples."""
    failures = [d for d in decisions if d.failure is not None]
    return {
        "decided": len(decisions),
        "actions": _counts(d.decision_action.value for d in decisions),
        "gate_status": _counts(d.gate_status for d in decisions),
        "matched_rules": _counts(rule for d in decisions for rule in d.matched_rules),
        "default_applied": sum(1 for d in decisions if d.default_applied),
        "failures": len(failures),
        "failure_sample": [d.as_dict() for d in failures[:SAMPLE_SIZE]],
        "sample": [d.as_dict() for d in decisions[:SAMPLE_SIZE]],
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


class DayRunStopped(Exception):  # noqa: N818 (a stop, not an error of the mock's)
    """The run stopped part way; ``run`` holds what the ledger answered.

    Attributes:
        run: Every row the ledger answered before the run stopped;
            ``run.stopped`` says why (the cause is chained).
    """

    def __init__(self, run: DayRun) -> None:
        """Carry the partial run."""
        super().__init__(run.stopped)
        self.run = run


async def registration_problem(
    client: LedgerClient,
    registration: ReasonerRegistration = MOCK_DAILY_REGISTRATION,
) -> str | None:
    """Why the ledger should not take this mock's day, or ``None``.

    The mock cites its bypass rule under its own registration; a ledger
    serving another version of it could find the cited rule does not hold
    and record the row under a HOLD floor. So a run first checks that the
    ledger serves this registration's version.

    Args:
        client: The ledger, as the mock's principal.
        registration: The registration the mock assembles under.

    Returns:
        ``None`` when the ledger serves the same ``registration_version``;
        otherwise what it serves instead.

    Raises:
        LedgerError: The ledger does not register the mock
            (``REASONER_NOT_FOUND``), or refused the request.
    """
    served = await client.reasoner(registration.reasoner_id)
    expected = registration_version(registration)
    if served.registration_version == expected:
        return None
    return (
        f"the ledger serves {registration.reasoner_id} at registration_version "
        f"{served.registration_version} ({served.source}); this mock's is "
        f"{expected}: register the current one (make registration-mock_daily, "
        "then PUT it, or restart the ledger with the current plugin or file)"
    )


async def run_day(
    settlements: Sequence[AccountSettlement], client: LedgerClient
) -> DayRun:
    """Compute a day and submit it, one row per request.

    Args:
        settlements: The day's rows, with distinct ``event_id``s.
        client: The ledger, as the mock's principal.

    Returns:
        What the ledger answered.

    Raises:
        ValueError: Two rows share an ``event_id``; nothing is sent.
        DayRunStopped: The ledger refused the key (401, 403), answered a
            server error (5xx), could not be reached, or a read of a decided
            row's bundle failed; it carries what the ledger answered until
            then (a row decided before its read failed among the decisions,
            from its receipt, and a row sent but not answered as
            ``in_doubt``).
    """
    computed = compute_day(settlements)
    run = DayRun(rows=len(computed))
    try:
        await _send(computed, client, run)
    except (LedgerError, httpx.HTTPError) as err:
        run.stopped = f"{type(err).__name__}: {err}"
        raise DayRunStopped(run) from err
    return run


async def _send(
    computed: Sequence[ComputedSettlement], client: LedgerClient, run: DayRun
) -> None:
    """Submit a computed day, keeping every answer in ``run`` as it comes."""
    for item in computed:
        event_id = item.submission.event_id
        try:
            receipt = await client.submit(item.submission)
        except LedgerError as err:
            if err.status in _KEY_REFUSED:
                raise  # every other row would be refused too
            if err.status >= 500:
                run.in_doubt = event_id  # the ledger may have decided it
                raise
            run.refused.append(Refusal(event_id, err.status, err.code, err.detail))
            continue
        except httpx.HTTPError as err:
            if not isinstance(err, _NOT_SENT):
                run.in_doubt = event_id  # sent, and not answered
            raise
        try:
            bundle = await client.get_bundle(receipt.decision_id)
        except (LedgerError, httpx.HTTPError):
            # The ledger decided the row: keep it, from its receipt, and stop.
            run.decisions.append(receipt_outcome(receipt, item.submission))
            raise
        run.decisions.append(decision_outcome(receipt, bundle))
