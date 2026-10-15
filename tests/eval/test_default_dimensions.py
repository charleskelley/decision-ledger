"""Tests for ``_build_default_dimensions()`` — the harness wiring layer.

Coverage scope:
- The framework harness builds its three reasoner-facing dimensions
  (consistency, robustness, shadow); all datasets missing → consistency and
  robustness become ``SkippedDimension`` entries that name the missing
  paths, while shadow (which needs no dataset) is built from the driver's
  framework services and runs after the others.
- Gate-owned dimensions are never built here, even when a reasoner ships
  datasets for them: each gate runs its own harness (DR-28).
- Dataset root defaults to the adapter's ``datasets_dir``; the driver comes
  from ``adapter.build_driver`` with the harness settings, and its
  configuration errors propagate (the CLI maps them to exit 2).

The reasoner is a fake ``EvalAdapter`` over the stub reasoner's event type,
so the harness wiring is exercised without any real reasoner installed. No
infrastructure, model, or LLM is touched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.eval.metrics import EvalDimension
from eval.adapter import EvalAdapter
from eval.dimensions.consistency import ConsistencyDimension, PipelineRunResult
from eval.dimensions.shadow import ShadowDimension
from eval.dimensions.skipped import SkippedDimension
from eval.runners import harness
from tests._stub_reasoner.events import StubEvent

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeAdapter:
    """In-test ``EvalAdapter``; records every ``build_driver`` call."""

    datasets_dir: Path
    reasoner_id: str = "stub-reasoner"
    event_type: type[StubEvent] = StubEvent
    driver: MagicMock = field(default_factory=MagicMock)
    built_with: list[FrameworkSettings] = field(default_factory=list)
    driver_error: Exception | None = None

    def build_driver(self, settings: FrameworkSettings) -> MagicMock:
        self.built_with.append(settings)
        if self.driver_error is not None:
            raise self.driver_error
        return self.driver


def _adapter(tmp_path: Path) -> _FakeAdapter:
    return _FakeAdapter(datasets_dir=tmp_path / "unused-default")


def test_fake_adapter_satisfies_the_eval_adapter_protocol(tmp_path: Path) -> None:
    assert isinstance(_adapter(tmp_path), EvalAdapter)


# ---------------------------------------------------------------------------
# All-missing-datasets path
# ---------------------------------------------------------------------------


def test_all_datasets_missing_skips_consistency_and_robustness(
    tmp_path: Path,
) -> None:
    """Empty dataset_root → consistency and robustness become SkippedDimension."""
    dimensions = harness._build_default_dimensions(
        adapter=_adapter(tmp_path),
        settings=FrameworkSettings(),
        dataset_root=tmp_path,
    )

    assert [d.kind for d in dimensions] == [
        EvalDimension.CONSISTENCY,
        EvalDimension.ROBUSTNESS,
        EvalDimension.SHADOW,
    ]
    assert all(isinstance(d, SkippedDimension) for d in dimensions[:2])
    assert isinstance(dimensions[2], ShadowDimension)


def test_the_shadow_dimension_samples_through_the_drivers_framework(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    dimensions = harness._build_default_dimensions(
        adapter=adapter, settings=FrameworkSettings(), dataset_root=tmp_path
    )
    shadow = dimensions[-1]
    assert isinstance(shadow, ShadowDimension)
    assert shadow.runs_after_others is True
    assert shadow._source is adapter.driver.framework.store
    assert shadow._gates is adapter.driver.framework.gates
    assert shadow._reasoner_id == "stub-reasoner"
    assert shadow._since is not None


def test_skipped_dimensions_carry_dataset_path_in_reason(tmp_path: Path) -> None:
    """Each skipped dimension's reason names the missing dataset location."""
    dimensions = harness._build_default_dimensions(
        adapter=_adapter(tmp_path),
        settings=FrameworkSettings(),
        dataset_root=tmp_path,
    )
    by_kind = {d.kind: d for d in dimensions}

    consistency_run = asyncio.run(by_kind[EvalDimension.CONSISTENCY].evaluate())
    assert "scenarios" in consistency_run.result.threshold_violations[0]

    robustness_run = asyncio.run(by_kind[EvalDimension.ROBUSTNESS].evaluate())
    assert "robustness" in robustness_run.result.threshold_violations[0]


def test_gate_owned_dimensions_are_never_built_by_the_framework_harness(
    tmp_path: Path,
) -> None:
    """A reasoner's datasets for a gate are that gate's harness's business."""
    for relative in (
        "retrieval/golden_queries.yaml",
        "faithfulness/golden_outputs.yaml",
        "citations/golden_outputs.yaml",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text("queries: []\ncases: []\n", encoding="utf-8")

    dimensions = harness._build_default_dimensions(
        adapter=_adapter(tmp_path),
        settings=FrameworkSettings(),
        dataset_root=tmp_path,
    )

    assert {d.kind for d in dimensions} == {
        EvalDimension.CONSISTENCY,
        EvalDimension.ROBUSTNESS,
        EvalDimension.SHADOW,
    }


# ---------------------------------------------------------------------------
# Adapter-driven defaults
# ---------------------------------------------------------------------------


def _write_scenario(directory: Path) -> None:
    event = StubEvent(
        event_id="e1", timestamp=datetime(2026, 1, 1, tzinfo=UTC), widget_id="w-1"
    ).model_dump_json()
    (directory / "scenarios").mkdir(parents=True)
    (directory / "scenarios" / "s.yaml").write_text(
        "scenarios:\n"
        "  - scenario_id: s1\n"
        "    description: typed\n"
        "    orderings:\n"
        f"      - {{ordering_id: a, events: [{event}]}}\n"
        f"      - {{ordering_id: b, events: [{event}]}}\n",
        encoding="utf-8",
    )


def test_dataset_root_defaults_to_the_adapters_datasets_dir(tmp_path: Path) -> None:
    """Without ``dataset_root``, datasets are read from the adapter."""
    adapter = _FakeAdapter(datasets_dir=tmp_path)
    _write_scenario(tmp_path)

    dimensions = harness._build_default_dimensions(
        adapter=adapter, settings=FrameworkSettings()
    )

    consistency = next(d for d in dimensions if d.kind == EvalDimension.CONSISTENCY)
    assert isinstance(consistency, ConsistencyDimension)


def test_driver_is_built_once_by_the_adapter_with_the_harness_settings(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    settings = FrameworkSettings()

    harness._build_default_dimensions(
        adapter=adapter, settings=settings, dataset_root=tmp_path
    )

    assert adapter.built_with == [settings]


def test_a_driver_configuration_error_propagates(tmp_path: Path) -> None:
    """The reasoner's driver owns its configuration checks (e.g. an API key)."""
    adapter = _adapter(tmp_path)
    adapter.driver_error = ValueError("OPENAI_API_KEY is required for the driver")

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        harness._build_default_dimensions(
            adapter=adapter, settings=FrameworkSettings(), dataset_root=tmp_path
        )


def test_scenario_events_are_validated_against_the_adapters_event_type(
    tmp_path: Path,
) -> None:
    """Consistency datasets are parsed with ``adapter.event_type``.

    The built dimension hands the adapter's driver events of that type.
    """
    _write_scenario(tmp_path)
    adapter = _adapter(tmp_path)
    adapter.driver.run = AsyncMock(
        return_value=PipelineRunResult(decision_action=DecisionAction.ALLOW)
    )

    dimensions = harness._build_default_dimensions(
        adapter=adapter, settings=FrameworkSettings(), dataset_root=tmp_path
    )
    consistency = next(d for d in dimensions if d.kind == EvalDimension.CONSISTENCY)
    assert isinstance(consistency, ConsistencyDimension)
    asyncio.run(consistency.evaluate())

    driven = [e for call in adapter.driver.run.await_args_list for e in call.args[0]]
    assert driven
    assert all(type(e) is StubEvent for e in driven)
