"""Per-account sliding state — the stream mock's feature store, in memory.

``AccountWindows`` keeps, for every account the stream has carried, its
recent transfers and the count and total of its older ones, and turns each
new transfer into the complete feature snapshot the scorer consumes.

Every feature runs on event time: a transfer's features read only the
account's transfers timestamped before it — the sliding window those within
``window`` before it, the baseline and the history all of them — so
precursors that arrived in any order give it the same features. A transfer
that arrives after another it precedes (within the source's lateness bound)
counts for the transfers scored after it arrives, never for one already
scored: a stream decides each event once, when it arrives.

The state is per process and not persisted: a restarted service starts with
every account fresh (``thin_history``). ``reset`` is real: it forgets every
account (the eval driver resets before each ordering of a consistency
scenario).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from mock_stream_reasoner.events import TransferEvent

#: How far back a transfer's window reaches (event time).
WINDOW = timedelta(minutes=10)
#: An account with fewer transfers than this before a transfer has a thin
#: history at that transfer: the registration's novel-entity feature (Tier 3).
THIN_HISTORY_EVENTS = 3
#: The baseline amount of an account with no history yet.
REFERENCE_AMOUNT = 100.0


@dataclass(frozen=True)
class TransferFeatures:
    """The feature snapshot of one transfer, read before it joins the state.

    Attributes:
        amount: The transfer's amount.
        baseline_amount: The mean amount of the account's transfers
            timestamped before it (``REFERENCE_AMOUNT`` when there are none).
        amount_ratio: ``amount / baseline_amount``.
        window_events: The account's transfers within the window before it.
        window_amount: Their total amount.
        prior_events: The account's transfers timestamped before it.
        thin_history: ``prior_events < THIN_HISTORY_EVENTS``.
    """

    amount: float
    baseline_amount: float
    amount_ratio: float
    window_events: int
    window_amount: float
    prior_events: int
    thin_history: bool

    def as_feature_set(self) -> dict[str, float | int | str | bool]:
        """Every feature by name: the ``ReasonerContext.feature_set``."""
        return {
            "amount": self.amount,
            "baseline_amount": self.baseline_amount,
            "amount_ratio": self.amount_ratio,
            "window_events": self.window_events,
            "window_amount": self.window_amount,
            "prior_events": self.prior_events,
            "thin_history": self.thin_history,
        }


@dataclass
class _Account:
    """One account's state: its retained transfers and the rest, summed."""

    recent: list[tuple[datetime, float]] = field(default_factory=list)
    older_count: int = 0
    older_total: float = 0.0
    latest: datetime | None = None


class AccountWindows:
    """Sliding per-account windows over the transfers the stream carries.

    Args:
        window: How far back a transfer's window reaches.
        retention: How long a transfer is kept whole behind the account's
            latest one; at least ``window``. A transfer that arrives within
            ``retention - window`` of the latest is scored exactly: its window
            is whole, and every transfer the state no longer keeps whole is
            older than it, counted in its history. The service passes the
            window plus the source's allowed lateness, the most an accepted
            event can trail; the default, twice the window, covers the
            default lateness.
    """

    def __init__(
        self, *, window: timedelta = WINDOW, retention: timedelta | None = None
    ) -> None:
        """Start with no accounts."""
        if window <= timedelta(0):
            msg = f"window must be positive, got {window}"
            raise ValueError(msg)
        self.window = window
        self.retention = max(retention if retention is not None else 2 * window, window)
        self._accounts: dict[str, _Account] = {}

    def __len__(self) -> int:
        """How many accounts the state holds."""
        return len(self._accounts)

    def observe(self, event: TransferEvent) -> TransferFeatures:
        """Read one transfer's features, then add it to its account's state.

        Args:
            event: The transfer, as the source delivered it.

        Returns:
            Its features, computed from the account's transfers timestamped
            before it only.
        """
        account = self._accounts.setdefault(event.account_id, _Account())
        at_event = event.timestamp
        before = [(at, amount) for at, amount in account.recent if at < at_event]
        prior = account.older_count + len(before)
        total = account.older_total + sum(amount for _, amount in before)
        start = at_event - self.window
        in_window = [amount for at, amount in before if at >= start]
        baseline = total / prior if prior else REFERENCE_AMOUNT
        features = TransferFeatures(
            amount=event.amount,
            baseline_amount=round(baseline, 2),
            amount_ratio=round(event.amount / baseline, 4),
            window_events=len(in_window),
            window_amount=round(float(sum(in_window)), 2),
            prior_events=prior,
            thin_history=prior < THIN_HISTORY_EVENTS,
        )
        account.recent.append((at_event, event.amount))
        latest = at_event if account.latest is None else max(account.latest, at_event)
        account.latest = latest
        horizon = latest - self.retention
        kept = [(at, amount) for at, amount in account.recent if at >= horizon]
        dropped = [amount for at, amount in account.recent if at < horizon]
        account.older_count += len(dropped)
        account.older_total += sum(dropped)
        account.recent = kept
        return features

    def reset(self) -> None:
        """Forget every account."""
        self._accounts.clear()
