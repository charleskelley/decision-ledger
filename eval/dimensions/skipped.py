"""SkippedDimension wrapper — placeholder for unwired dimensions.

Conforms to the ``Dimension`` protocol so the harness can list all five
expected dimensions in the ``EvalReport`` even when one or more aren't
yet configured (typically because their golden dataset hasn't been
curated). Returns ``passed=False`` with a clear ``"skipped: <reason>"``
entry in ``threshold_violations``.

A skipped dimension counts as a failure for ``overall_passed`` —
missing fixtures = not ready to release.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.eval.metrics import DimensionResult, EvalDimension
from eval.dimensions import DimensionRun


class SkippedDimension:
    """Placeholder ``Dimension`` that reports as skipped without running.

    Args:
        kind: Which of the five canonical dimensions this slot
            represents in the ``EvalReport``. Assigned as an instance
            attribute (not a property) so the class structurally
            satisfies the ``Dimension`` protocol.
        reason: Human-readable reason for the skip (e.g.,
            ``"dataset missing: eval/datasets/faithfulness/golden_outputs.yaml"``).
            Surfaces in ``threshold_violations``.
    """

    def __init__(self, *, kind: EvalDimension, reason: str) -> None:
        """Construct a skipped placeholder for one dimension slot."""
        self.kind = kind
        self._reason = reason

    async def evaluate(self) -> DimensionRun:
        """Return a synthetic ``DimensionRun`` marking the slot as skipped."""
        return DimensionRun(
            result=DimensionResult(
                dimension=self.kind,
                passed=False,
                metrics={},
                threshold_violations=[f"skipped: {self._reason}"],
                evaluated_at=datetime.now(UTC),
                num_samples=0,
            ),
            metrics=None,
        )
