"""The daily mock's computation — pure arithmetic, not a model.

``compute_drift`` turns one account's settlement into the evidence a
``ReasonerContext`` carries: how far the actual amount drifted from the
target, and every value the drift was computed from::

    drift_amount  = actual_amount - target_amount            (to the cent)
    drift_pct     = 100 * (actual_amount - target_amount) / target_amount
    abs_drift_pct = |drift_pct|                              (both to 4 places)

``drift_pct`` is the decision's numerical label: negative when less settled
than the books expect, positive when more did. The bypass rule and the rule
set read ``abs_drift_pct``: an account within tolerance is recorded, and the
rules gate decides the rest by the size of the drift, whichever its sign.

The result is a function of the row alone, and the same row always gives the
same result: nothing is carried from one row or one day to the next, so a
day decides each account the same whatever order its rows arrive in, and the
bundle's feature set holds everything the computation read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mock_daily_reasoner.settlements import AccountSettlement

#: Version recorded as the ``ReasonerContext.model_version``.
COMPUTE_VERSION = "settlement-drift-v1"


@dataclass(frozen=True)
class SettlementDrift:
    """The computation's output for one row.

    Attributes:
        target_amount: What the books expect to settle.
        actual_amount: What was settled.
        drift_amount: ``actual_amount - target_amount``, to the cent.
        drift_pct: The drift as a percentage of the target, to four places;
            negative for a shortfall.
        abs_drift_pct: ``|drift_pct|``.
    """

    target_amount: float
    actual_amount: float
    drift_amount: float
    drift_pct: float
    abs_drift_pct: float

    @property
    def feature_set(self) -> dict[str, float | int | str | bool]:
        """Every value by name: the ``ReasonerContext.feature_set``."""
        return {
            "target_amount": self.target_amount,
            "actual_amount": self.actual_amount,
            "drift_amount": self.drift_amount,
            "drift_pct": self.drift_pct,
            "abs_drift_pct": self.abs_drift_pct,
        }

    @property
    def narrative(self) -> str:
        """The arithmetic with this row's numbers (the bundle's attribution)."""
        return (
            f"drift_pct = 100 * (actual_amount - target_amount) / target_amount "
            f"= 100 * ({self.actual_amount} - {self.target_amount}) / "
            f"{self.target_amount} = {self.drift_pct}"
        )


def compute_drift(settlement: AccountSettlement) -> SettlementDrift:
    """Compute one row's drift. Pure and deterministic.

    Args:
        settlement: The account's settlement for the day.

    Returns:
        The drift and the amounts it was computed from.
    """
    target = settlement.target_amount
    actual = settlement.actual_amount
    drift_pct = round(100.0 * (actual - target) / target, 4)
    return SettlementDrift(
        target_amount=target,
        actual_amount=actual,
        drift_amount=round(actual - target, 2),
        drift_pct=drift_pct,
        abs_drift_pct=abs(drift_pct),
    )
