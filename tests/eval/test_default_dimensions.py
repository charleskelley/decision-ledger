"""Tests for ``_build_default_dimensions()`` — the harness wiring layer.

Coverage scope:
- ``OPENAI_API_KEY`` and ``ANTHROPIC_API_KEY`` fail-fast (no
  infrastructure required).
- All datasets missing → 5 ``SkippedDimension`` entries (heavy clients
  monkey-patched).
- Retrieval dataset present → a ``RetrievalDimension`` scoped to the
  adapter's ``reasoner_id``.
- Dataset root defaults to the adapter's ``datasets_dir``; the driver comes
  from ``adapter.build_driver``.

The reasoner is a fake ``EvalAdapter`` over the stub reasoner's event type,
so the harness wiring is exercised without any real reasoner installed.

Live-stack dimension construction (real PolicyRetriever, OpenAI/Anthropic
calls, RAGAS init) is exercised by the Step 6 scenario smoke test against
a running Docker stack.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.eval.metrics import EvalDimension
from eval.adapter import EvalAdapter
from eval.dimensions.consistency import ConsistencyDimension, PipelineRunResult
from eval.dimensions.retrieval import RetrievalDimension
from eval.dimensions.skipped import SkippedDimension
from eval.runners import harness
from tests._stub_reasoner.events import StubEvent


@dataclass
class _FakeAdapter:
    """In-test ``EvalAdapter``; records every ``build_driver`` call."""

    datasets_dir: Path
    reasoner_id: str = "stub-reasoner"
    event_type: type[StubEvent] = StubEvent
    driver: MagicMock = field(default_factory=MagicMock)
    built_with: list[FrameworkSettings] = field(default_factory=list)

    def build_driver(self, settings: FrameworkSettings) -> MagicMock:
        self.built_with.append(settings)
        return self.driver


def _adapter(tmp_path: Path) -> _FakeAdapter:
    return _FakeAdapter(datasets_dir=tmp_path / "unused-default")


def _keys() -> FrameworkSettings:
    return FrameworkSettings(openai_api_key="sk-test", anthropic_api_key="sk-ant-test")


def test_fake_adapter_satisfies_the_eval_adapter_protocol(tmp_path: Path) -> None:
    assert isinstance(_adapter(tmp_path), EvalAdapter)


# ---------------------------------------------------------------------------
# OPENAI_API_KEY fail-fast (no infrastructure or monkeypatching needed)
# ---------------------------------------------------------------------------


def test_raises_when_openai_api_key_missing():
    """Empty OPENAI_API_KEY raises ValueError before any client construction."""
    settings = FrameworkSettings(openai_api_key="", anthropic_api_key="sk-ant-test")
    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        harness._build_default_dimensions(
            adapter=_FakeAdapter(datasets_dir=Path()), settings=settings
        )


def test_raises_when_anthropic_api_key_missing():
    """Empty ANTHROPIC_API_KEY raises ValueError before any client construction.

    The faithfulness/citation judges run on Anthropic to reduce in-family
    bias against the OpenAI-backed gate; both keys are required.
    """
    settings = FrameworkSettings(openai_api_key="sk-test", anthropic_api_key="")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
        harness._build_default_dimensions(
            adapter=_FakeAdapter(datasets_dir=Path()), settings=settings
        )


# ---------------------------------------------------------------------------
# All-missing-datasets path (heavy clients monkey-patched)
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_heavy_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub AnthropicLLMClient and RagasFaithfulnessAdapter.

    The pipeline driver comes from the fake adapter, so together these let
    ``_build_default_dimensions`` run end-to-end without opening any
    connections, instantiating an Anthropic client, or initializing RAGAS.
    (AnthropicLLMClient default-constructs AsyncAnthropic internally —
    mocking the client class itself is sufficient.)
    """
    monkeypatch.setattr(harness, "AnthropicLLMClient", MagicMock())
    monkeypatch.setattr(harness, "RagasFaithfulnessAdapter", MagicMock())


def test_all_datasets_missing_returns_five_skipped_dimensions(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """Empty dataset_root → all 5 dimensions become SkippedDimension."""
    dimensions = harness._build_default_dimensions(
        adapter=_adapter(tmp_path),
        settings=_keys(),
        dataset_root=tmp_path,
    )

    assert len(dimensions) == 5
    assert all(isinstance(d, SkippedDimension) for d in dimensions)


def test_all_datasets_missing_covers_each_canonical_kind(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """The 5 SkippedDimensions cover each EvalDimension exactly once."""
    dimensions = harness._build_default_dimensions(
        adapter=_adapter(tmp_path),
        settings=_keys(),
        dataset_root=tmp_path,
    )

    kinds = {d.kind for d in dimensions}
    assert kinds == {
        EvalDimension.RETRIEVAL,
        EvalDimension.FAITHFULNESS,
        EvalDimension.CONSISTENCY,
        EvalDimension.CITATION,
        EvalDimension.ROBUSTNESS,
    }


def test_skipped_dimensions_carry_dataset_path_in_reason(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """Each skipped dimension's reason names the missing dataset path."""
    dimensions = harness._build_default_dimensions(
        adapter=_adapter(tmp_path),
        settings=_keys(),
        dataset_root=tmp_path,
    )

    by_kind = {d.kind: d for d in dimensions}

    # Probe by evaluating each skipped dimension and reading the violation
    retrieval_run = asyncio.run(by_kind[EvalDimension.RETRIEVAL].evaluate())
    assert "golden_queries.yaml" in retrieval_run.result.threshold_violations[0]

    faithfulness_run = asyncio.run(by_kind[EvalDimension.FAITHFULNESS].evaluate())
    assert "golden_outputs.yaml" in faithfulness_run.result.threshold_violations[0]

    consistency_run = asyncio.run(by_kind[EvalDimension.CONSISTENCY].evaluate())
    assert "scenarios" in consistency_run.result.threshold_violations[0]

    robustness_run = asyncio.run(by_kind[EvalDimension.ROBUSTNESS].evaluate())
    assert "robustness" in robustness_run.result.threshold_violations[0]


# ---------------------------------------------------------------------------
# Retrieval dimension wiring (dataset present)
# ---------------------------------------------------------------------------


def test_retrieval_dimension_is_scoped_to_the_adapters_reasoner_id(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """The harness scopes golden queries to the adapter's reasoner.

    Production retrieval always carries ``reasoner_id``; the eval dimension
    must exercise the same tenant-filtered path rather than an unfiltered
    one that hides mapping regressions.
    """
    adapter = _adapter(tmp_path)
    retrieval_dir = tmp_path / "retrieval"
    retrieval_dir.mkdir()
    (retrieval_dir / "golden_queries.yaml").write_text(
        "queries:\n"
        "  - query_id: q01\n"
        "    query: 'find policy'\n"
        "    relevant_document_ids: ['DOC-A']\n"
        "    expected_top_document_id: DOC-A\n"
        "    expected_top_version: '1.0'\n",
        encoding="utf-8",
    )

    dimensions = harness._build_default_dimensions(
        adapter=adapter,
        settings=_keys(),
        dataset_root=tmp_path,
    )

    retrieval = next(d for d in dimensions if d.kind == EvalDimension.RETRIEVAL)
    assert isinstance(retrieval, RetrievalDimension)
    assert retrieval.reasoner_id == adapter.reasoner_id


# ---------------------------------------------------------------------------
# Adapter-driven defaults
# ---------------------------------------------------------------------------


def test_dataset_root_defaults_to_the_adapters_datasets_dir(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """Without ``dataset_root``, datasets are read from the adapter."""
    adapter = _FakeAdapter(datasets_dir=tmp_path)
    (tmp_path / "retrieval").mkdir()
    (tmp_path / "retrieval" / "golden_queries.yaml").write_text(
        "queries:\n"
        "  - query_id: q01\n"
        "    query: 'find policy'\n"
        "    relevant_document_ids: ['DOC-A']\n"
        "    expected_top_document_id: DOC-A\n"
        "    expected_top_version: '1.0'\n",
        encoding="utf-8",
    )

    dimensions = harness._build_default_dimensions(adapter=adapter, settings=_keys())

    retrieval = next(d for d in dimensions if d.kind == EvalDimension.RETRIEVAL)
    assert isinstance(retrieval, RetrievalDimension)


def test_driver_is_built_once_by_the_adapter_with_the_harness_settings(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    settings = _keys()

    harness._build_default_dimensions(
        adapter=adapter, settings=settings, dataset_root=tmp_path
    )

    assert adapter.built_with == [settings]


def test_scenario_events_are_validated_against_the_adapters_event_type(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """Consistency datasets are parsed with ``adapter.event_type``.

    The built dimension hands the adapter's driver events of that type.
    """
    event = StubEvent(
        event_id="e1", timestamp=datetime(2026, 1, 1, tzinfo=UTC), widget_id="w-1"
    ).model_dump_json()
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "s.yaml").write_text(
        "scenarios:\n"
        "  - scenario_id: s1\n"
        "    description: typed\n"
        "    orderings:\n"
        f"      - {{ordering_id: a, events: [{event}]}}\n"
        f"      - {{ordering_id: b, events: [{event}]}}\n",
        encoding="utf-8",
    )

    adapter = _adapter(tmp_path)
    adapter.driver.run = AsyncMock(
        return_value=PipelineRunResult(decision_action=DecisionAction.ALLOW)
    )

    dimensions = harness._build_default_dimensions(
        adapter=adapter, settings=_keys(), dataset_root=tmp_path
    )
    consistency = next(d for d in dimensions if d.kind == EvalDimension.CONSISTENCY)
    assert isinstance(consistency, ConsistencyDimension)
    asyncio.run(consistency.evaluate())

    driven = [e for call in adapter.driver.run.await_args_list for e in call.args[0]]
    assert driven
    assert all(type(e) is StubEvent for e in driven)
