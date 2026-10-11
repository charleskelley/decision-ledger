"""Behavioral tests for the eval harness CLI ``main()``.

Covers the exit-code and side-effect contracts of the CLI surface:

- ``--reasoner-id`` (or ``DECISIONLEDGER_EVAL_REASONER``) is required; a
  missing or unknown reasoner is a config error (exit 2, no report).
- Exit 2 + no report file when no dimensions are registered.
- Exit 0 when every dimension passes thresholds.
- Exit 1 when any dimension fails its thresholds.
- ``--output`` flag overrides the default staging path.

Stub dimensions are minimal and local — they don't depend on the
``test_harness.py`` stubs, so the file stays self-contained. Adapter
resolution is replaced by a sentinel except in the tests that exercise it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import EntryPoint
from typing import TYPE_CHECKING, Any

import pytest

from core.eval.metrics import DimensionResult, EvalDimension
from eval import adapter as eval_adapter
from eval.adapter import EVAL_ADAPTER_GROUP
from eval.dimensions import DimensionRun
from eval.runners import harness

if TYPE_CHECKING:
    from pathlib import Path

_REASONER = "stub-reasoner"
_ARGS = ["--reasoner-id", _REASONER]
_ADAPTER = object()


@pytest.fixture(autouse=True)
def resolved_adapter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Resolve every reasoner id to a sentinel adapter; record the ids asked for."""
    asked: list[str] = []

    def _load(reasoner_id: str) -> object:
        asked.append(reasoner_id)
        return _ADAPTER

    monkeypatch.setattr(harness, "load_eval_adapter", _load)
    monkeypatch.delenv("DECISIONLEDGER_EVAL_REASONER", raising=False)
    return asked


def _dims(*dims: Any):
    """A ``_build_default_dimensions`` replacement that checks the adapter."""

    def _build(*, adapter: object) -> list[Any]:
        assert adapter is _ADAPTER
        return list(dims)

    return _build


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
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims())
    output_path = tmp_path / "report.json"

    rc = harness.main([*_ARGS, "--output", str(output_path)])

    assert rc == 2


def test_main_does_not_write_report_when_no_dimensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The empty-dimensions failure must not pollute the output path."""
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims())
    output_path = tmp_path / "report.json"

    harness.main([*_ARGS, "--output", str(output_path)])

    assert not output_path.exists()


def test_main_exits_2_when_build_dimensions_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ValueError from _build_default_dimensions (e.g., missing OPENAI_API_KEY)
    surfaces as exit 2 with a clean stderr message — not an unhandled traceback.
    """

    def _raise(*, adapter: object) -> list:
        msg = "OPENAI_API_KEY is required"
        raise ValueError(msg)

    monkeypatch.setattr(harness, "_build_default_dimensions", _raise)
    output_path = tmp_path / "report.json"

    rc = harness.main([*_ARGS, "--output", str(output_path)])

    assert rc == 2
    assert not output_path.exists()


def test_main_exits_0_when_all_dimensions_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Single passing dimension produces overall_passed=True and exit 0."""
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims(_PassingStub()))
    output_path = tmp_path / "report.json"

    rc = harness.main([*_ARGS, "--output", str(output_path)])

    assert rc == 0
    assert output_path.exists()


def test_main_exits_1_when_a_dimension_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing dimension propagates to overall_passed=False and exit 1."""
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims(_FailingStub()))
    output_path = tmp_path / "report.json"

    rc = harness.main([*_ARGS, "--output", str(output_path)])

    assert rc == 1
    assert output_path.exists()


def test_main_writes_report_to_output_flag_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--output PATH creates parent directories and writes the report there."""
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims(_PassingStub()))
    nested_path = tmp_path / "nested" / "subdir" / "report.json"

    rc = harness.main([*_ARGS, "--output", str(nested_path)])

    assert rc == 0
    assert nested_path.exists()
    assert nested_path.parent.is_dir()


# ---------------------------------------------------------------------------
# Reasoner selection
# ---------------------------------------------------------------------------


def test_main_exits_2_when_no_reasoner_is_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither ``--reasoner-id`` nor the env var: config error, no report."""
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims(_PassingStub()))
    output_path = tmp_path / "report.json"

    rc = harness.main(["--output", str(output_path)])

    assert rc == 2
    assert not output_path.exists()


def test_main_reads_the_reasoner_from_the_env_var_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resolved_adapter: list[str]
) -> None:
    monkeypatch.setenv("DECISIONLEDGER_EVAL_REASONER", "env-reasoner")
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims(_PassingStub()))

    rc = harness.main(["--output", str(tmp_path / "report.json")])

    assert rc == 0
    assert resolved_adapter == ["env-reasoner"]


def test_main_flag_wins_over_the_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resolved_adapter: list[str]
) -> None:
    monkeypatch.setenv("DECISIONLEDGER_EVAL_REASONER", "env-reasoner")
    monkeypatch.setattr(harness, "_build_default_dimensions", _dims(_PassingStub()))

    harness.main([*_ARGS, "--output", str(tmp_path / "report.json")])

    assert resolved_adapter == [_REASONER]


def test_main_exits_2_and_names_installed_adapters_when_reasoner_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An id with no eval adapter is a config error that lists what exists."""
    monkeypatch.setattr(harness, "load_eval_adapter", eval_adapter.load_eval_adapter)
    monkeypatch.setattr(
        eval_adapter,
        "metadata",
        _FakeMetadata([_entry_point("other-reasoner")]),
    )
    output_path = tmp_path / "report.json"

    rc = harness.main(
        ["--reasoner-id", "no-such-reasoner", "--output", str(output_path)]
    )

    assert rc == 2
    assert not output_path.exists()
    err = capsys.readouterr().err
    assert "no-such-reasoner" in err
    assert "other-reasoner" in err


def _entry_point(name: str) -> EntryPoint:
    return EntryPoint(name, "tests._stub_reasoner.plugin:PLUGIN", EVAL_ADAPTER_GROUP)


class _FakeMetadata:
    """Stands in for ``importlib.metadata`` inside ``eval.adapter``."""

    def __init__(self, eps: list[EntryPoint]) -> None:
        self._eps = eps

    def entry_points(self, *, group: str) -> list[EntryPoint]:
        return [ep for ep in self._eps if ep.group == group]
