"""Behavioral tests for the eval harness CLI ``main()``.

Covers the five exit-code and side-effect contracts of the CLI surface:

- Exit 2 + no report file when no dimensions are registered.
- Exit 0 when every dimension passes thresholds.
- Exit 1 when any dimension fails its thresholds.
- ``--output`` flag overrides the default staging path.

Stub dimensions are minimal and local — they don't depend on the
``test_harness.py`` stubs, so the file stays self-contained.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.eval.metrics import DimensionResult, EvalDimension
from eval.dimensions import DimensionRun
from eval.runners import harness

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# Minimal local stubs
# ---------------------------------------------------------------------------


class _PassingStub:
    """A dimension that always returns ``passed=True``."""

    kind = EvalDimension.RETRIEVAL

    async def evaluate(self) -> DimensionRun:
        result = DimensionResult(
            dimension=EvalDimension.RETRIEVAL,
            passed=True,
            metrics={},
            threshold_violations=[],
            evaluated_at=datetime.now(UTC),
            num_samples=1,
        )
        return DimensionRun(result=result, metrics=None)


class _FailingStub:
    """A dimension that always returns ``passed=False``."""

    kind = EvalDimension.RETRIEVAL

    async def evaluate(self) -> DimensionRun:
        result = DimensionResult(
            dimension=EvalDimension.RETRIEVAL,
            passed=False,
            metrics={},
            threshold_violations=["stub failure"],
            evaluated_at=datetime.now(UTC),
            num_samples=1,
        )
        return DimensionRun(result=result, metrics=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_main_exits_2_when_no_dimensions_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty dimension set is a config error: exit 2, never run the harness."""
    monkeypatch.setattr(harness, "_build_default_dimensions", list)
    output_path = tmp_path / "report.json"

    rc = harness.main(["--output", str(output_path)])

    assert rc == 2


def test_main_does_not_write_report_when_no_dimensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The empty-dimensions failure must not pollute the output path."""
    monkeypatch.setattr(harness, "_build_default_dimensions", list)
    output_path = tmp_path / "report.json"

    harness.main(["--output", str(output_path)])

    assert not output_path.exists()


def test_main_exits_0_when_all_dimensions_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Single passing dimension produces overall_passed=True and exit 0."""
    monkeypatch.setattr(harness, "_build_default_dimensions", lambda: [_PassingStub()])
    output_path = tmp_path / "report.json"

    rc = harness.main(["--output", str(output_path)])

    assert rc == 0
    assert output_path.exists()


def test_main_exits_1_when_a_dimension_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing dimension propagates to overall_passed=False and exit 1."""
    monkeypatch.setattr(harness, "_build_default_dimensions", lambda: [_FailingStub()])
    output_path = tmp_path / "report.json"

    rc = harness.main(["--output", str(output_path)])

    assert rc == 1
    assert output_path.exists()


def test_main_writes_report_to_output_flag_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--output PATH creates parent directories and writes the report there."""
    monkeypatch.setattr(harness, "_build_default_dimensions", lambda: [_PassingStub()])
    nested_path = tmp_path / "nested" / "subdir" / "report.json"

    rc = harness.main(["--output", str(nested_path)])

    assert rc == 0
    assert nested_path.exists()
    assert nested_path.parent.is_dir()
