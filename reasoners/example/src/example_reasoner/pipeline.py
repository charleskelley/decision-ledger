"""The example's pipeline — score an inspection and submit it to the ledger.

``decide_widget`` is the whole reasoner loop: score, assemble the
``Submission``, and hand it to the ledger through a ``LedgerClient``
(DR-31) — ``LocalClient`` when the ledger runs in this process,
``HttpClient`` when it is a service. The ledger runs the gate set, enforces,
writes the bundle, and returns the receipt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from example_reasoner.assembler import build_submission
from example_reasoner.scorer import score_widget

if TYPE_CHECKING:
    from app.api.models import DecisionReceipt
    from app.client import LedgerClient
    from example_reasoner.events import WidgetEvent


async def decide_widget(event: WidgetEvent, *, client: LedgerClient) -> DecisionReceipt:
    """Score one inspection and submit it.

    Args:
        event: The inspection.
        client: The ledger.

    Returns:
        The ledger's receipt.

    Raises:
        LedgerError: The ledger refused the submission.
    """
    return await client.submit(build_submission(event, score_widget(event)))
