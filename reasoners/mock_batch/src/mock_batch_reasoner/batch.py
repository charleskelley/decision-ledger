"""The batch mock's run — a day's rows into the ledger.

``run_day`` scores every row and assembles its submission (``assembler``),
then:

1. sends every recorded row to ``POST /api/v1/decisions:batch``
   (``LedgerClient.submit_batch``), in row order, in chunks of at most
   ``MAX_BATCH_LINES`` lines (10,000, the most the ledger takes in one
   request), and keeps every line's result;
2. submits every exception — a row no bypass rule covers — on its own, in
   gate mode (``LedgerClient.submit``), where the rules gate decides it, and
   reads the gate's verdict back from the decision's bundle.

A batch is record-only: the ledger rejects a gate-mode line on its own with
``BATCH_RECORD_ONLY`` and records the rest. The mock provokes that once per
run, deliberately (``provoke_record_only``): its first exception rides in a
chunk, at its row's place among the records; the ledger rejects that one
line, and the mock submits the row through ``submit`` like every other
exception. A line the ledger rejects for any other reason is reported, not
retried, and so is an exception the ledger refuses. A refused batch request
(a missing or wrong key, for example) or a ledger that cannot be reached
stops the run: ``run_day`` raises ``DayRunStopped`` with everything the
ledger answered until then.

A cited bypass rule that does not hold is not a rejection: the ledger records
that line under a HOLD floor (``bypass_rule_holds: false``, decision-level
rule ``bypass_rule_not_satisfied``), so it is recorded as HOLD, or as BLOCK
when the rule it cites records BLOCK. The mock cites only a rule that holds
under its own registration; a ledger serving another version of it may
disagree, which ``registration_problem`` checks before a run and the run's
report counts (``cited_rule_not_held``).

The ledger does not deduplicate: every line it records is a new decision,
whatever its ``event_id``. A day's rows must have distinct ids, and a day
sent twice is recorded twice.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from core.exceptions import LedgerError
from core.observation import registration_version
from mock_batch_reasoner.assembler import RULES_GATE_ID, build_submission
from mock_batch_reasoner.registry import MOCK_BATCH_REGISTRATION
from mock_batch_reasoner.scorer import score_row

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from app.api.models import BatchLineResult, DecisionReceipt
    from app.client import LedgerClient
    from core.actions import DecisionAction
    from core.bundle import DecisionBundle
    from core.observation import ReasonerRegistration
    from core.submission import Submission
    from mock_batch_reasoner.rows import AccountDay
    from mock_batch_reasoner.scorer import DelinquencyScore

#: The most lines the ledger takes in one batch request.
MAX_BATCH_LINES = 10_000
#: The code of a gate-mode line in a batch.
BATCH_RECORD_ONLY = "BATCH_RECORD_ONLY"
#: How many decided exceptions a run's report lists.
SAMPLE_SIZE = 5


@dataclass(frozen=True)
class ScoredRow:
    """One row, its score, and the submission assembled from them.

    Attributes:
        row: The account's end-of-day position.
        score: Its score.
        submission: Recorded when a bypass rule covers the row, gated
            otherwise.
    """

    row: AccountDay
    score: DelinquencyScore
    submission: Submission

    @property
    def is_exception(self) -> bool:
        """No bypass rule covers the row: the rules gate decides it."""
        return self.submission.mode == "gate"


def score_rows(rows: Iterable[AccountDay]) -> list[ScoredRow]:
    """Score every row and assemble its submission, in row order.

    Args:
        rows: The day's rows.

    Returns:
        One ``ScoredRow`` per row.

    Raises:
        ValueError: Two rows share an ``event_id``.
    """
    scored: list[ScoredRow] = []
    seen: set[str] = set()
    for row in rows:
        if row.event_id in seen:
            msg = f"two rows share the event id {row.event_id!r}"
            raise ValueError(msg)
        seen.add(row.event_id)
        score = score_row(row)
        scored.append(ScoredRow(row, score, build_submission(row, score)))
    return scored


@dataclass(frozen=True)
class DayPlan:
    """What a run sends.

    Attributes:
        chunks: The batch requests' lines, in row order: every recorded row,
            and the provoked exception at its row's place.
        exceptions: The rows submitted on their own, in row order (the
            provoked one too, once the ledger has rejected its line).
        provoked: The ``event_id`` of the exception sent in a batch, or
            ``None``.
    """

    chunks: tuple[tuple[Submission, ...], ...]
    exceptions: tuple[Submission, ...]
    provoked: str | None


def plan_day(
    scored: Sequence[ScoredRow],
    *,
    chunk_size: int = MAX_BATCH_LINES,
    provoke_record_only: bool = True,
) -> DayPlan:
    """Split a scored day into batch requests and exceptions.

    Args:
        scored: The day's scored rows.
        chunk_size: Lines per batch request, from 1 to ``MAX_BATCH_LINES``.
        provoke_record_only: Send the first exception in a batch request, to
            be rejected with ``BATCH_RECORD_ONLY``.

    Returns:
        The plan.

    Raises:
        ValueError: ``chunk_size`` is outside 1 to ``MAX_BATCH_LINES``.
    """
    if not 1 <= chunk_size <= MAX_BATCH_LINES:
        msg = f"chunk_size must be from 1 to {MAX_BATCH_LINES}, got {chunk_size}"
        raise ValueError(msg)
    exceptions = [s.submission for s in scored if s.is_exception]
    provoked = exceptions[0].event_id if provoke_record_only and exceptions else None
    lines = [
        s.submission
        for s in scored
        if not s.is_exception or s.submission.event_id == provoked
    ]
    return DayPlan(
        chunks=tuple(
            tuple(lines[start : start + chunk_size])
            for start in range(0, len(lines), chunk_size)
        ),
        exceptions=tuple(exceptions),
        provoked=provoked,
    )


@dataclass(frozen=True)
class LineOutcome:
    """One batch line's result, with the request it was sent in.

    Attributes:
        request: The batch request, numbered from 1.
        result: The ledger's result for the line (its ``line`` counts within
            the request).
    """

    request: int
    result: BatchLineResult

    def as_dict(self) -> dict[str, Any]:
        """The outcome as JSON-ready data."""
        return {"request": self.request, **self.result.model_dump(mode="json")}


@dataclass(frozen=True)
class ExceptionOutcome:
    """An exception the ledger decided through the rules gate.

    Attributes:
        event_id: The row's id.
        decision_id: The decision.
        decision_action: The action the ledger decided.
        decision_level_rule: The decision-level rule that fired, if any.
        matched_rules: The rule-set rules that matched (the gate's verdict).
        default_applied: No rule matched and the rule set's default applied;
            ``None`` when the gate gave no verdict.
        failure: Why the rules gate gave no verdict (its ``extra.failure``,
            the reason a failed gate is recorded with, or ``"no verdict"``);
            ``None`` when it gave one.
        provoked: The row was first sent in a batch request.
    """

    event_id: str
    decision_id: str
    decision_action: DecisionAction
    decision_level_rule: str | None
    matched_rules: tuple[str, ...]
    default_applied: bool | None
    failure: str | None
    provoked: bool

    def as_dict(self) -> dict[str, Any]:
        """The outcome as JSON-ready data."""
        return {
            "event_id": self.event_id,
            "decision_id": self.decision_id,
            "decision_action": self.decision_action.value,
            "decision_level_rule": self.decision_level_rule,
            "matched_rules": list(self.matched_rules),
            "default_applied": self.default_applied,
            "failure": self.failure,
            "provoked": self.provoked,
        }


def exception_outcome(
    receipt: DecisionReceipt, bundle: DecisionBundle, *, provoked: bool
) -> ExceptionOutcome:
    """Read the rules gate's verdict on one exception from its bundle.

    Args:
        receipt: What ``submit`` returned.
        bundle: The decision's bundle.
        provoked: The row was first sent in a batch request.

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
    return ExceptionOutcome(
        event_id=receipt.event_id,
        decision_id=receipt.decision_id,
        decision_action=receipt.decision_action,
        decision_level_rule=receipt.decision_level_rule,
        matched_rules=tuple(str(rule) for rule in matched)
        if isinstance(matched, list)
        else (),
        default_applied=bool(default) if has_verdict else None,
        failure=str(failure) if failure is not None else None,
        provoked=provoked,
    )


@dataclass(frozen=True)
class Refusal:
    """An exception the ledger refused (``submit`` answered with a problem).

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
        provoked: The ``event_id`` of the exception sent in a batch, or
            ``None``.
        requests: Each batch request's lines, recorded, and rejected.
        lines: Every batch line's outcome, in the order sent.
        exceptions: Every exception the ledger decided, in row order.
        refused: Every exception the ledger refused.
        stopped: Why the run stopped part way (the error), or ``None`` when
            it ran to the end.
    """

    rows: int
    provoked: str | None = None
    requests: list[dict[str, int]] = field(default_factory=list)
    lines: list[LineOutcome] = field(default_factory=list)
    exceptions: list[ExceptionOutcome] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    stopped: str | None = None

    @property
    def recorded(self) -> list[LineOutcome]:
        """The lines the ledger recorded."""
        return [o for o in self.lines if o.result.status == "recorded"]

    @property
    def rejected(self) -> list[LineOutcome]:
        """The lines the ledger rejected."""
        return [o for o in self.lines if o.result.status == "rejected"]

    @property
    def decided(self) -> int:
        """Rows the ledger decided: recorded lines and decided exceptions."""
        return len(self.recorded) + len(self.exceptions)

    @property
    def undecided(self) -> int:
        """Rows the ledger did not decide (rejected, refused, or never sent)."""
        return self.rows - self.decided

    def _is_provoked(self, outcome: LineOutcome) -> bool:
        return self.provoked is not None and outcome.result.event_id == self.provoked

    def action_of(self, event_id: str) -> DecisionAction | None:
        """The action the ledger decided for one row, if it decided it."""
        for outcome in self.lines:
            if outcome.result.event_id == event_id and outcome.result.status == (
                "recorded"
            ):
                return outcome.result.decision_action
        for exception in self.exceptions:
            if exception.event_id == event_id:
                return exception.decision_action
        return None

    def summary(self) -> dict[str, Any]:
        """The run's report, JSON-ready: counts by action, rule, and code.

        Lists are capped at ``SAMPLE_SIZE`` entries (the provoked rejection
        always shown); every line and exception is in ``records()``.
        """
        recorded = self.recorded
        rejected = self.rejected
        first_per_action: dict[str, dict[str, Any]] = {}
        for outcome in recorded:
            action = outcome.result.decision_action
            if action is not None and action.value not in first_per_action:
                first_per_action[action.value] = outcome.as_dict()
        shown = [o for o in rejected if self._is_provoked(o)]
        shown += [o for o in rejected if o not in shown][: SAMPLE_SIZE - len(shown)]
        failures = [e for e in self.exceptions if e.failure is not None]
        return {
            "rows": self.rows,
            "decided": self.decided,
            "undecided": self.undecided,
            "stopped": self.stopped,
            "batch": {
                "requests": self.requests,
                "lines": len(self.lines),
                "recorded": len(recorded),
                "rejected": len(rejected),
                "actions": _counts(
                    o.result.decision_action.value
                    for o in recorded
                    if o.result.decision_action is not None
                ),
                "cited_rule_not_held": sum(
                    1 for o in recorded if o.result.bypass_rule_holds is False
                ),
                "rejected_codes": _counts(o.result.code or "" for o in rejected),
                "rejections": [
                    {**o.as_dict(), "provoked": self._is_provoked(o)} for o in shown
                ],
                "recorded_sample": [
                    first_per_action[action] for action in sorted(first_per_action)
                ],
            },
            "exceptions": {
                "decided": len(self.exceptions),
                "refused": len(self.refused),
                "refused_sample": [r.as_dict() for r in self.refused[:SAMPLE_SIZE]],
                "actions": _counts(e.decision_action.value for e in self.exceptions),
                "matched_rules": _counts(
                    rule for e in self.exceptions for rule in e.matched_rules
                ),
                "default_applied": sum(1 for e in self.exceptions if e.default_applied),
                "failures": len(failures),
                "failure_sample": [e.as_dict() for e in failures[:SAMPLE_SIZE]],
                "sample": [e.as_dict() for e in self.exceptions[:SAMPLE_SIZE]],
            },
        }

    def records(self) -> list[dict[str, Any]]:
        """Every batch line and every exception, JSON-ready, for a results file.

        Returns:
            One ``{"kind": "line", ...}`` per batch line, in the order sent,
            then one ``{"kind": "exception", ...}`` per decided exception and
            one ``{"kind": "refused", ...}`` per refused one.
        """
        return [
            *({"kind": "line", **o.as_dict()} for o in self.lines),
            *({"kind": "exception", **e.as_dict()} for e in self.exceptions),
            *({"kind": "refused", **r.as_dict()} for r in self.refused),
        ]


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


class DayRunStopped(Exception):  # noqa: N818 (a stop, not an error of the mock's)
    """The run stopped part way; ``run`` holds what the ledger answered.

    Attributes:
        run: Every request and exception the ledger answered before the run
            stopped; ``run.stopped`` says why (the cause is chained).
    """

    def __init__(self, run: DayRun) -> None:
        """Carry the partial run."""
        super().__init__(run.stopped)
        self.run = run


async def registration_problem(
    client: LedgerClient,
    registration: ReasonerRegistration = MOCK_BATCH_REGISTRATION,
) -> str | None:
    """Why the ledger should not take this mock's day, or ``None``.

    The mock picks each row's bypass rule under its own registration; a
    ledger serving another version of it could find a cited rule that does
    not hold and record the line under a HOLD floor. So a run first checks
    that the ledger serves this registration's version.

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
        f"{expected}: register the current one (make registration-mock_batch, "
        "then PUT it, or restart the ledger with the current plugin or file)"
    )


async def run_day(
    rows: Sequence[AccountDay],
    client: LedgerClient,
    *,
    chunk_size: int = MAX_BATCH_LINES,
    provoke_record_only: bool = True,
) -> DayRun:
    """Score a day and submit it: records in batches, exceptions on their own.

    Args:
        rows: The day's rows, with distinct ``event_id``s.
        client: The ledger, as the mock's principal.
        chunk_size: Lines per batch request, from 1 to ``MAX_BATCH_LINES``.
        provoke_record_only: Send the first exception in a batch request, to
            be rejected with ``BATCH_RECORD_ONLY`` and then submitted.

    Returns:
        What the ledger answered.

    Raises:
        ValueError: Two rows share an ``event_id``, or ``chunk_size`` is out
            of range; nothing is sent.
        DayRunStopped: The ledger refused a whole batch request (a missing
            or wrong key, for example), a read of a decided exception, or
            could not be reached; it carries what the ledger answered until
            then.
    """
    scored = score_rows(rows)
    plan = plan_day(
        scored, chunk_size=chunk_size, provoke_record_only=provoke_record_only
    )
    run = DayRun(rows=len(scored), provoked=plan.provoked)
    try:
        await _send(plan, client, run)
    except (LedgerError, httpx.HTTPError) as err:
        run.stopped = f"{type(err).__name__}: {err}"
        raise DayRunStopped(run) from err
    return run


async def _send(plan: DayPlan, client: LedgerClient, run: DayRun) -> None:
    """Send a planned day, keeping every answer in ``run`` as it comes."""
    for request, chunk in enumerate(plan.chunks, start=1):
        result = await client.submit_batch(chunk)
        run.requests.append(
            {
                "lines": len(chunk),
                "recorded": result.recorded,
                "rejected": result.rejected,
            }
        )
        run.lines.extend(LineOutcome(request, line) for line in result.results)
    rerouted = {
        o.result.event_id
        for o in run.rejected
        if o.result.code == BATCH_RECORD_ONLY and o.result.event_id == plan.provoked
    }
    for submission in plan.exceptions:
        provoked = submission.event_id == plan.provoked
        if provoked and submission.event_id not in rerouted:
            # Its line was not rejected as record-only: the line's result is
            # the row's outcome, reported as it is, and never sent twice.
            continue
        try:
            receipt = await client.submit(submission)
        except LedgerError as err:
            run.refused.append(
                Refusal(submission.event_id, err.status, err.code, err.detail)
            )
            continue
        bundle = await client.get_bundle(receipt.decision_id)
        run.exceptions.append(exception_outcome(receipt, bundle, provoked=provoked))
