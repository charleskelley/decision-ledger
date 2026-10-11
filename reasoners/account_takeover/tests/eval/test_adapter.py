"""The ATO ``EvalAdapter`` satisfies the framework eval contract."""

from __future__ import annotations

import importlib.metadata

from ato_reasoner.eval.adapter import ADAPTER
from ato_reasoner.events import LoginEvent
from ato_reasoner.registry import ATO_REGISTRATION
from eval.adapter import EVAL_ADAPTER_GROUP, EvalAdapter, load_eval_adapter

_DIMENSION_DIRS = {"retrieval", "faithfulness", "scenarios", "citations", "robustness"}


def test_ato_adapter_satisfies_the_eval_adapter_protocol():
    assert isinstance(ADAPTER, EvalAdapter)


def test_ato_adapter_id_matches_the_registration():
    assert ADAPTER.reasoner_id == ATO_REGISTRATION.reasoner_id


def test_ato_adapter_datasets_are_written_in_login_events():
    assert ADAPTER.event_type is LoginEvent


def test_ato_datasets_dir_holds_one_folder_per_dimension():
    assert ADAPTER.datasets_dir.is_dir()
    present = {p.name for p in ADAPTER.datasets_dir.iterdir() if p.is_dir()}
    assert present >= _DIMENSION_DIRS


def test_ato_adapter_is_installed_under_the_eval_adapter_entry_point_group():
    names = {
        ep.name for ep in importlib.metadata.entry_points(group=EVAL_ADAPTER_GROUP)
    }
    assert ATO_REGISTRATION.reasoner_id in names
    assert load_eval_adapter(ATO_REGISTRATION.reasoner_id) is ADAPTER
