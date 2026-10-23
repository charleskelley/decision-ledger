"""The stream mock's loop — decide every transfer the stream delivers.

``StreamProcessor`` is the reasoner. For each event the source accepted it
reads the account's features from the sliding state, scores them, assembles
the ``Submission``, and submits it through a ``LedgerClient`` (an
``HttpClient`` in the mock's service; a ``LocalClient`` in tests and the
eval driver). The ledger runs the gate set, enforces, writes the bundle, and
returns the receipt.

The state learns a transfer when it is read, before the ledger answers: the
transfer happened whatever the ledger decides. What the ledger refuses
(``rejected``), cannot be reached for (``unreachable``), or what fails on the
way (``failed``) is dead-lettered on the source, with why, and the stream
goes on: one bad event never stops the consumer.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import structlog

from core.exceptions import LedgerError
from mock_stream_reasoner.assembler import build_submission
from mock_stream_reasoner.scorer import score_transfer
from mock_stream_reasoner.state import AccountWindows

if TYPE_CHECKING:
    from app.api.models import DecisionReceipt
    from app.client import LedgerClient
    from mock_stream_reasoner.events import TransferEvent
    from mock_stream_reasoner.source import DeadLetterReason, EventSource

log = structlog.get_logger(__name__)

#: How many decisions the processor keeps for ``decisions`` (newest last).
HISTORY = 1_000


@dataclass(frozen=True)
class StreamDecision:
    """One decided transfer, as the processor remembers it.

    Attributes:
        event_id: The transfer.
        account_id: Its account.
        decision_id: The ledger's decision.
        mode: ``record`` or ``gate``.
        decision_action: The ledger's final action.
        bypass_rule_id: The rule a record cited.
        decision_level_rule: The decision-level enforcement rule that fired
            (``tier3_novel_entity`` for a fresh account held for review).
        thin_history: Whether the account's history was thin.
    """

    event_id: str
    account_id: str
    decision_id: str
    mode: str
    decision_action: str
    bypass_rule_id: str | None
    decision_level_rule: str | None
    thin_history: bool


async def decide_transfer(
    event: TransferEvent, *, windows: AccountWindows, client: LedgerClient
) -> tuple[DecisionReceipt, bool]:
    """Read one transfer's features, score it, and submit it.

    Args:
        event: The transfer.
        windows: The per-account state (it learns the transfer).
        client: The ledger.

    Returns:
        The ledger's receipt, and whether the account's history was thin.

    Raises:
        LedgerError: The ledger refused the submission.
        httpx.HTTPError: The ledger could not be reached.
    """
    score = score_transfer(windows.observe(event))
    receipt = await client.submit(build_submission(event, score))
    return receipt, score.features.thin_history


class StreamProcessor:
    """Decides each accepted transfer and dead-letters what it cannot.

    Args:
        client: The ledger the decisions go to.
        windows: The per-account state; a fresh one by default.
        history: How many decisions ``decisions`` keeps.
    """

    def __init__(
        self,
        client: LedgerClient,
        *,
        windows: AccountWindows | None = None,
        history: int = HISTORY,
    ) -> None:
        """Bind the client and the state."""
        self.client = client
        self.windows = windows if windows is not None else AccountWindows()
        self.history = max(history, 1)
        self._decisions: deque[StreamDecision] = deque(maxlen=self.history)
        self._counts: Counter[str] = Counter()

    @property
    def decisions(self) -> tuple[StreamDecision, ...]:
        """The most recent decisions, oldest first."""
        return tuple(self._decisions)

    def stats(self) -> dict[str, int]:
        """Counters since the processor started.

        Returns:
            ``decided`` and ``dead_lettered``, then one counter per mode
            (``mode_<mode>``), final action (``action_<ACTION>``), and
            decision-level rule that fired (``rule_<rule>``).
        """
        return dict(sorted(self._counts.items()))

    async def process(
        self, event: TransferEvent, source: EventSource
    ) -> StreamDecision | None:
        """Decide one transfer; dead-letter it on ``source`` when that fails.

        Args:
            event: An event ``source`` accepted.
            source: Where a transfer that cannot be decided is dead-lettered.

        Returns:
            The decision, or ``None`` when the transfer was dead-lettered.
        """
        try:
            receipt, thin = await decide_transfer(
                event, windows=self.windows, client=self.client
            )
        except LedgerError as err:
            reason: DeadLetterReason = (
                "unreachable" if err.status >= 500 else "rejected"
            )
            self._dead_letter(
                source,
                event,
                reason,
                f"{err.status} {err.code}: {err.detail}",
            )
            return None
        except httpx.HTTPError as err:
            self._dead_letter(
                source,
                event,
                "unreachable",
                f"the ledger could not be reached ({type(err).__name__})",
            )
            return None
        except Exception as err:  # one bad event never stops the stream
            self._dead_letter(
                source, event, "failed", f"the decision failed ({type(err).__name__})"
            )
            log.exception(
                "mock_stream.decision_failed",
                component="mock_stream",
                event_id=event.event_id,
                error_type=type(err).__name__,
            )
            return None
        decision = StreamDecision(
            event_id=event.event_id,
            account_id=event.account_id,
            decision_id=receipt.decision_id,
            mode=receipt.mode,
            decision_action=receipt.decision_action.value,
            bypass_rule_id=receipt.bypass_rule_id,
            decision_level_rule=receipt.decision_level_rule,
            thin_history=thin,
        )
        self._decisions.append(decision)
        self._counts["decided"] += 1
        self._counts[f"mode_{decision.mode}"] += 1
        self._counts[f"action_{decision.decision_action}"] += 1
        if decision.decision_level_rule is not None:
            self._counts[f"rule_{decision.decision_level_rule}"] += 1
        log.info(
            "mock_stream.decided",
            component="mock_stream",
            event_id=event.event_id,
            decision_id=receipt.decision_id,
            mode=receipt.mode,
            decision_action=decision.decision_action,
            decision_level_rule=decision.decision_level_rule,
        )
        return decision

    async def run(self, source: EventSource) -> None:
        """Decide every event ``source`` delivers until it closes.

        Args:
            source: The stream.
        """
        async for event in source.events():
            await self.process(event, source)

    def _dead_letter(
        self,
        source: EventSource,
        event: TransferEvent,
        reason: DeadLetterReason,
        detail: str,
    ) -> None:
        source.dead_letter(
            reason,
            event_id=event.event_id,
            detail=detail,
            payload=event.payload(),
        )
        self._counts["dead_lettered"] += 1
        log.warning(
            "mock_stream.dead_lettered",
            component="mock_stream",
            event_id=event.event_id,
            reason=reason,
            detail=detail,
        )
