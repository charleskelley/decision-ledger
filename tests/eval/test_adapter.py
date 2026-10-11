"""Behavior of ``eval.adapter.load_eval_adapter`` — reasoner eval discovery.

Adapters are published as in-memory ``importlib.metadata.EntryPoint`` objects
whose values point back into this module, so discovery is exercised end to end
(``ep.load()`` included) without installing a package or naming a real
reasoner.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import EntryPoint
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.exceptions import ReasonerPluginError
from eval.adapter import EVAL_ADAPTER_GROUP, EvalAdapter, load_eval_adapter
from tests._stub_reasoner.events import StubEvent
from tests._stub_reasoner.registry import STUB_REGISTRATION

_MODULE = __name__


@dataclass(frozen=True)
class _StubEvalAdapter:
    """Minimal ``EvalAdapter`` over the stub reasoner."""

    reasoner_id: str
    event_type: type[StubEvent] = StubEvent
    datasets_dir: Path = Path(__file__).resolve().parent

    def build_driver(self, settings: object) -> MagicMock:
        del settings
        return MagicMock()


STUB_ADAPTER = _StubEvalAdapter(STUB_REGISTRATION.reasoner_id)
OTHER_ADAPTER = _StubEvalAdapter("other-reasoner")
#: Declares ``reasoner_id`` and ``event_type`` but no ``datasets_dir`` or
#: ``build_driver`` — not an ``EvalAdapter``.
NOT_AN_ADAPTER = object()


def _ep(name: str, attr: str) -> EntryPoint:
    return EntryPoint(name=name, value=f"{_MODULE}:{attr}", group=EVAL_ADAPTER_GROUP)


STUB_EP = _ep(STUB_REGISTRATION.reasoner_id, "STUB_ADAPTER")
OTHER_EP = _ep("other-reasoner", "OTHER_ADAPTER")


def test_stub_eval_adapter_satisfies_the_protocol():
    assert isinstance(STUB_ADAPTER, EvalAdapter)


def test_loads_the_adapter_published_under_the_requested_id():
    adapter = load_eval_adapter(
        STUB_REGISTRATION.reasoner_id, entry_points=[OTHER_EP, STUB_EP]
    )

    assert adapter is STUB_ADAPTER
    assert adapter.event_type is StubEvent


def test_unknown_id_raises_and_lists_the_installed_adapters():
    with pytest.raises(ReasonerPluginError) as exc_info:
        load_eval_adapter("no-such-reasoner", entry_points=[STUB_EP, OTHER_EP])

    message = str(exc_info.value)
    assert "no-such-reasoner" in message
    assert STUB_REGISTRATION.reasoner_id in message
    assert "other-reasoner" in message


def test_unknown_id_with_nothing_installed_says_so():
    with pytest.raises(ReasonerPluginError, match="none"):
        load_eval_adapter("no-such-reasoner", entry_points=[])


def test_object_that_is_not_an_eval_adapter_is_rejected():
    ep = _ep("broken-reasoner", "NOT_AN_ADAPTER")

    with pytest.raises(ReasonerPluginError, match="EvalAdapter protocol"):
        load_eval_adapter("broken-reasoner", entry_points=[ep])


def test_entry_point_name_must_equal_the_adapters_reasoner_id():
    ep = _ep("renamed-reasoner", "STUB_ADAPTER")

    with pytest.raises(ReasonerPluginError, match="must equal"):
        load_eval_adapter("renamed-reasoner", entry_points=[ep])


def test_entry_point_that_fails_to_import_is_reported():
    ep = EntryPoint(
        name="ghost-reasoner",
        value="tests._no_such_module:ADAPTER",
        group=EVAL_ADAPTER_GROUP,
    )

    with pytest.raises(ReasonerPluginError, match="failed to load"):
        load_eval_adapter("ghost-reasoner", entry_points=[ep])


def test_two_adapters_claiming_one_id_is_an_error():
    twin = _ep(STUB_REGISTRATION.reasoner_id, "OTHER_ADAPTER")

    with pytest.raises(ReasonerPluginError, match="multiple"):
        load_eval_adapter(STUB_REGISTRATION.reasoner_id, entry_points=[STUB_EP, twin])
