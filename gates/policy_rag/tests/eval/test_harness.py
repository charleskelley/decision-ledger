"""Behavioral tests for the policy gate's eval harness wiring and CLI.

The gate owns its eval (DR-28): ``python -m policy_rag_gate.eval`` runs its
retrieval, faithfulness, and citation dimensions against the golden datasets
a reasoner ships for the gate, through the framework's ``run_eval``.

- Adapter selection: one named reasoner, or every installed adapter that
  ships at least one of the gate's datasets.
- Dimension wiring: a dataset present → the real dimension (retrieval scoped
  to the reasoner); missing → a ``SkippedDimension`` naming the path.
- CLI: config errors (nothing to evaluate, unknown reasoner, missing API
  keys) exit 2 without opening any store; a run writes one report per
  reasoner under the per-gate staging path, closes what it opened, and exits
  1 when a dimension fails or is skipped, 0 when all pass.

No Postgres, Elasticsearch, model, or LLM is touched; no dimension that
would call an LLM is ever evaluated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from core.eval.metrics import DimensionResult, EvalDimension
from core.exceptions import ReasonerPluginError
from eval.adapter import EVAL_ADAPTER_GROUP
from eval.dimensions import DimensionRun
from eval.dimensions.skipped import SkippedDimension
from policy_rag_gate.eval import harness
from policy_rag_gate.eval.citation import CitationDimension
from policy_rag_gate.eval.faithfulness import FaithfulnessDimension
from policy_rag_gate.eval.retrieval import RetrievalDimension

_QUERIES = (
    "queries:\n"
    "  - query_id: q01\n"
    "    query: 'find policy'\n"
    "    relevant_document_ids: ['DOC-A']\n"
    "    expected_top_document_id: DOC-A\n"
    "    expected_top_version: '1.0'\n"
)
_FAITHFULNESS = (
    "cases:\n"
    "  - case_id: f01\n"
    "    rationale: 'Held for review.'\n"
    "    contexts: ['Widgets SHALL be held.']\n"
    "    cited_snippets: ['Widgets SHALL be held.']\n"
)
_CITATIONS = (
    "cases:\n"
    "  - case_id: c01\n"
    "    claim: 'Widgets are held.'\n"
    "    document_id: DOC-A\n"
    "    document_version: '1.0'\n"
    "    section_path: '2. Review'\n"
    "    snippet_text: 'Widgets SHALL be held.'\n"
)


class _Event(BaseModel):
    widget_id: str


@dataclass
class _Adapter:
    reasoner_id: str
    datasets_dir: Path
    event_type: type[BaseModel] = _Event
    built: list[Any] = field(default_factory=list)

    def build_driver(self, settings: Any) -> Any:
        self.built.append(settings)
        raise AssertionError("the gate's eval never drives a reasoner pipeline")


def _write(root: Path, relative: Path, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry_points(*names: str) -> list[EntryPoint]:
    return [EntryPoint(name, "unused:ADAPTER", EVAL_ADAPTER_GROUP) for name in names]


@pytest.fixture
def adapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, _Adapter]:
    """Two installed adapters: ``with-datasets`` ships one, ``bare`` none."""
    installed = {
        "with-datasets": _Adapter("with-datasets", tmp_path / "with-datasets"),
        "bare": _Adapter("bare", tmp_path / "bare"),
    }
    _write(installed["with-datasets"].datasets_dir, harness.RETRIEVAL_DATASET, _QUERIES)

    def _load(reasoner_id: str, *, entry_points: Any = None) -> _Adapter:
        del entry_points
        try:
            return installed[reasoner_id]
        except KeyError:
            raise ReasonerPluginError(reasoner_id, "no eval adapter") from None

    monkeypatch.setattr(harness, "load_eval_adapter", _load)
    return installed


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------


def test_ships_gate_datasets_needs_any_one_of_the_three(tmp_path: Path):
    assert not harness.ships_gate_datasets(tmp_path)
    _write(tmp_path, harness.CITATION_DATASET, _CITATIONS)
    assert harness.ships_gate_datasets(tmp_path)


def test_without_a_reasoner_every_adapter_shipping_datasets_is_selected(adapters):
    selected = harness.select_adapters(
        None, entry_points=_entry_points("bare", "with-datasets")
    )

    assert selected == [adapters["with-datasets"]]


def test_a_named_reasoner_is_selected_even_without_datasets(adapters):
    selected = harness.select_adapters("bare", entry_points=_entry_points("bare"))

    assert selected == [adapters["bare"]]


def test_an_unknown_reasoner_is_a_plugin_error(adapters):
    with pytest.raises(ReasonerPluginError):
        harness.select_adapters("ghost", entry_points=_entry_points("bare"))


# ---------------------------------------------------------------------------
# Dimension wiring
# ---------------------------------------------------------------------------


def _build(dataset_root: Path) -> list[Any]:
    return harness.build_gate_dimensions(
        reasoner_id="with-datasets",
        dataset_root=dataset_root,
        retriever=MagicMock(),
        judge_client=MagicMock(),
        ragas_scorer=MagicMock(),
    )


def test_missing_datasets_become_skipped_dimensions_naming_the_path(tmp_path: Path):
    dimensions = _build(tmp_path)

    assert [d.kind for d in dimensions] == [
        EvalDimension.RETRIEVAL,
        EvalDimension.FAITHFULNESS,
        EvalDimension.CITATION,
    ]
    assert all(isinstance(d, SkippedDimension) for d in dimensions)
    assert "golden_queries.yaml" in dimensions[0]._reason
    assert "faithfulness" in dimensions[1]._reason
    assert "citations" in dimensions[2]._reason


def test_present_datasets_build_the_real_dimensions(tmp_path: Path):
    _write(tmp_path, harness.RETRIEVAL_DATASET, _QUERIES)
    _write(tmp_path, harness.FAITHFULNESS_DATASET, _FAITHFULNESS)
    _write(tmp_path, harness.CITATION_DATASET, _CITATIONS)

    retrieval, faithfulness, citation = _build(tmp_path)

    assert isinstance(retrieval, RetrievalDimension)
    assert retrieval.reasoner_id == "with-datasets"
    assert isinstance(faithfulness, FaithfulnessDimension)
    assert isinstance(citation, CitationDimension)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the gate's stores and LLM clients; record what was opened."""
    opened: dict[str, Any] = {"calls": [], "resources": (MagicMock(), MagicMock())}

    def _open_retriever(**kwargs: Any) -> tuple[Any, Any]:
        opened["calls"].append(kwargs)
        return MagicMock(name="retriever"), opened["resources"]

    monkeypatch.setattr(harness, "open_retriever", _open_retriever)
    monkeypatch.setattr(harness, "AnthropicLLMClient", MagicMock())
    monkeypatch.setattr(harness, "RagasFaithfulnessAdapter", MagicMock())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("DECISIONLEDGER_EVAL_REASONER", raising=False)
    return opened


def _installed(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    eps = _entry_points(*names)
    monkeypatch.setattr(harness.metadata, "entry_points", lambda *, group: list(eps))


def test_main_exits_2_when_no_installed_reasoner_ships_datasets(
    adapters, stores, monkeypatch, tmp_path, capsys
):
    _installed(monkeypatch, "bare")

    rc = harness.main(["--output-dir", str(tmp_path / "out")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "no installed reasoner eval adapter ships datasets" in err
    assert stores["calls"] == []


def test_main_exits_2_for_an_unknown_reasoner(adapters, stores, monkeypatch, tmp_path):
    _installed(monkeypatch, "bare")

    rc = harness.main(["--reasoner-id", "ghost", "--output-dir", str(tmp_path)])

    assert rc == 2
    assert stores["calls"] == []


@pytest.mark.parametrize("missing", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
def test_main_exits_2_before_opening_a_store_when_a_key_is_missing(
    adapters, stores, monkeypatch, tmp_path, capsys, missing
):
    _installed(monkeypatch, "with-datasets")
    monkeypatch.setenv(missing, "")

    rc = harness.main(["--output-dir", str(tmp_path)])

    assert rc == 2
    assert missing in capsys.readouterr().err
    assert stores["calls"] == []


def test_main_writes_one_report_per_reasoner_and_closes_the_stores(
    adapters, stores, monkeypatch, tmp_path
):
    _installed(monkeypatch, "bare", "with-datasets")
    # Keep every dimension skipped: no LLM-backed dimension is evaluated.
    adapters["with-datasets"].datasets_dir = tmp_path / "empty"
    monkeypatch.setattr(harness, "ships_gate_datasets", lambda d: True)
    out = tmp_path / "out"

    rc = harness.main(["--output-dir", str(out)])

    # Skipped dimensions count as failures.
    assert rc == 1
    for reasoner_id in ("bare", "with-datasets"):
        report = json.loads((out / reasoner_id / "eval-report.json").read_text())
        assert report["overall_passed"] is False
        assert {d["dimension"] for d in report["dimensions"]} == {
            "retrieval",
            "faithfulness",
            "citation",
        }
    (call,) = stores["calls"]
    assert set(call) == {"postgres_dsn", "elasticsearch_url"}
    for resource in stores["resources"]:
        resource.close.assert_called_once()
    assert adapters["bare"].built == []


class _Passing:
    kind = EvalDimension.RETRIEVAL

    async def evaluate(self) -> DimensionRun:
        return DimensionRun(
            result=DimensionResult(
                dimension=EvalDimension.RETRIEVAL,
                passed=True,
                metrics={},
                threshold_violations=[],
                evaluated_at=datetime.now(UTC),
                num_samples=1,
            )
        )


def test_main_exits_0_when_every_report_passes_and_stages_per_gate_by_default(
    adapters, stores, monkeypatch, tmp_path
):
    _installed(monkeypatch, "with-datasets")
    monkeypatch.setattr(harness, "build_gate_dimensions", lambda **_: [_Passing()])
    monkeypatch.chdir(tmp_path)

    rc = harness.main([])

    assert rc == 0
    staged = tmp_path / harness.DEFAULT_OUTPUT_DIR / "with-datasets"
    assert (staged / "eval-report.json").is_file()
    assert Path("outputs/stage/eval/gate/policy") == harness.DEFAULT_OUTPUT_DIR


def test_the_env_var_names_the_reasoner_when_the_flag_is_absent(
    adapters, stores, monkeypatch, tmp_path
):
    _installed(monkeypatch, "bare", "with-datasets")
    monkeypatch.setenv("DECISIONLEDGER_EVAL_REASONER", "bare")
    monkeypatch.setattr(harness, "build_gate_dimensions", lambda **_: [_Passing()])

    out = tmp_path / "out"

    rc = harness.main(["--output-dir", str(out)])

    assert rc == 0
    assert (out / "bare" / "eval-report.json").is_file()
    assert not (out / "with-datasets").exists()
