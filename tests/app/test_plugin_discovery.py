"""Behavioral tests for reasoner plugin discovery and the app factory.

A second reasoner must be composable with zero framework changes. The stub
reasoner under ``tests/_stub_reasoner`` is that second reasoner: it is
published here as an in-memory ``importlib.metadata.EntryPoint`` named
``stub-reasoner`` (value ``tests._stub_reasoner.plugin:PLUGIN``), exactly
the shape an installed package declares, and composed through
``create_app(plugins=[...])``:

- Discovery loads the stub (and any other declared plugin), sorted by
  entry-point name.
- The allowlist filters by ``reasoner_id`` without loading skipped plugins.
- An entry-point name that differs from ``registration.reasoner_id``, a
  duplicate ``reasoner_id``, and an object that does not satisfy
  ``ReasonerPlugin`` each raise ``ReasonerPluginError``.
- ``create_app(plugins=[PLUGIN])`` mounts ``/api/v1/stub/*`` next to the
  framework routes without starting the lifespan; ``create_app()`` with no
  argument runs discovery with the ``DECISIONLEDGER_REASONERS`` allowlist.
- Starting the lifespan (with ``build_framework_services`` replaced by a
  fake) builds each plugin's services against the framework, exposes them
  on ``app.state``, closes reasoners before the framework on shutdown,
  cleans up when a plugin fails to build, and warns when no plugin is
  composed. No infrastructure is touched.
- ``create_app`` hands explicit gate plugins to the bootstrap unchanged and,
  without them, composes every installed gate (``decisionledger.gates``).

The multi-plugin lifecycle cases need a second, instrumented plugin; the
small ``_FakePlugin`` below provides it (with a close-order journal). It is
deliberately not another stub package — the stub is the exemplar, the fake
is scaffolding.
"""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from app.main import create_app
from app.plugin import FrameworkServices, ReasonerPlugin
from app.plugins import (
    PLUGIN_GROUP,
    allowlist_from_settings,
    build_registry,
    discover_plugins,
)
from app.settings import FrameworkSettings
from core.exceptions import ReasonerPluginError
from core.observation import ReasonerRegistration
from tests._fake_gate.plugin import FakeGatePlugin
from tests._stub_reasoner.plugin import PLUGIN, StubServices
from tests._stub_reasoner.registry import STUB_REGISTRATION

_HERE = Path(__file__).resolve().parent
_MODULE = __name__

#: The entry point an installed stub package would declare.
STUB_EP = EntryPoint(
    name=STUB_REGISTRATION.reasoner_id,
    value="tests._stub_reasoner.plugin:PLUGIN",
    group=PLUGIN_GROUP,
)


# ---------------------------------------------------------------------------
# Instrumented fake plugin (second tenant for the multi-plugin cases)
# ---------------------------------------------------------------------------


class _FakeEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    gadget_id: str


class _FakeServices:
    """Reasoner services that record when they are closed.

    ``journal`` is a shared list the lifespan tests use to assert close
    order across reasoners and the framework.
    """

    def __init__(self, reasoner_id: str, journal: list[str] | None = None) -> None:
        self.reasoner_id = reasoner_id
        self.closed = False
        self._journal = journal

    def close(self) -> None:
        self.closed = True
        if self._journal is not None:
            self._journal.append(self.reasoner_id)


def _registration(reasoner_id: str) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=f"{reasoner_id} (fake)",
        allowed_gate_ids=frozenset({"policy"}),
    )


class _FakePlugin:
    def __init__(self, reasoner_id: str, prefix: str) -> None:
        self.registration = _registration(reasoner_id)
        self.router = APIRouter(prefix=prefix, tags=[reasoner_id])
        self.prompts_dir = _HERE / "prompts"
        self.corpus_dir = _HERE / "corpus"
        self.raw_event_factory = _FakeEvent.model_validate
        self.built_with: FrameworkServices | None = None
        self.services: _FakeServices | None = None
        self.journal: list[str] | None = None

        @self.router.post("/decisions")
        def _decide(event: _FakeEvent) -> dict[str, Any]:
            return {"gadget_id": event.gadget_id}

    def build_services(self, framework: FrameworkServices) -> _FakeServices:
        self.built_with = framework
        self.services = _FakeServices(self.registration.reasoner_id, self.journal)
        return self.services


class _ExplodingPlugin(_FakePlugin):
    """A plugin whose service construction fails at startup."""

    def build_services(self, framework: FrameworkServices) -> _FakeServices:
        raise RuntimeError(f"{self.registration.reasoner_id} cannot start")


ALPHA = _FakePlugin("alpha-reasoner", "/api/v1/alpha")
#: Same id as the stub, published under a different entry-point name/value.
STUB_TWIN = _FakePlugin(STUB_REGISTRATION.reasoner_id, "/api/v1/stub-twin")


def _ep(name: str, attr: str) -> EntryPoint:
    return EntryPoint(name=name, value=f"{_MODULE}:{attr}", group=PLUGIN_GROUP)


# ---------------------------------------------------------------------------
# The stub reasoner satisfies the plugin contract
# ---------------------------------------------------------------------------


def test_stub_plugin_satisfies_the_reasoner_plugin_protocol():
    assert isinstance(PLUGIN, ReasonerPlugin)
    assert isinstance(ALPHA, ReasonerPlugin)


def test_stub_plugin_points_at_real_prompt_and_corpus_directories():
    assert sorted(p.name for p in PLUGIN.prompts_dir.glob("*.yaml")) == ["stub-v1.yaml"]
    assert sorted(p.name for p in PLUGIN.corpus_dir.glob("*.md")) == [
        "stub-policy-a.md",
        "stub-policy-b.md",
    ]


# ---------------------------------------------------------------------------
# discover_plugins
# ---------------------------------------------------------------------------


def test_discovery_loads_the_stub_through_its_entry_point():
    plugins = discover_plugins(entry_points=[STUB_EP])

    assert plugins == [PLUGIN]
    assert plugins[0].registration is STUB_REGISTRATION


def test_discovery_returns_plugins_sorted_by_entry_point_name():
    eps = [STUB_EP, _ep("alpha-reasoner", "ALPHA")]

    plugins = discover_plugins(entry_points=eps)

    assert plugins == [ALPHA, PLUGIN]


def test_discovery_with_no_entry_points_returns_empty_list():
    assert discover_plugins(entry_points=[]) == []


def test_allowlist_filters_by_reasoner_id():
    eps = [_ep("alpha-reasoner", "ALPHA"), STUB_EP]

    plugins = discover_plugins(entry_points=eps, allow=frozenset({"stub-reasoner"}))

    assert plugins == [PLUGIN]


def test_allowlist_skips_entries_without_loading_them():
    """A skipped entry point is never loaded, so a broken one cannot fail startup."""
    eps = [
        STUB_EP,
        EntryPoint(name="broken", value="no.such.module:THING", group=PLUGIN_GROUP),
    ]

    plugins = discover_plugins(entry_points=eps, allow=frozenset({"stub-reasoner"}))

    assert plugins == [PLUGIN]


def test_entry_point_name_must_equal_reasoner_id():
    eps = [EntryPoint(name="wrong-name", value=STUB_EP.value, group=PLUGIN_GROUP)]

    with pytest.raises(ReasonerPluginError, match="must equal registration"):
        discover_plugins(entry_points=eps)


def test_duplicate_reasoner_id_raises():
    eps = [STUB_EP, _ep(STUB_REGISTRATION.reasoner_id, "STUB_TWIN")]

    with pytest.raises(ReasonerPluginError, match="duplicate reasoner_id"):
        discover_plugins(entry_points=eps)


def test_non_conforming_object_raises():
    # A registration is a real object from the stub package, but it is not a
    # plugin: no router, no dirs, no build_services.
    eps = [
        EntryPoint(
            name="stub-reasoner",
            value="tests._stub_reasoner.registry:STUB_REGISTRATION",
            group=PLUGIN_GROUP,
        )
    ]

    with pytest.raises(ReasonerPluginError, match="does not satisfy"):
        discover_plugins(entry_points=eps)


def test_unloadable_entry_point_raises_plugin_error():
    eps = [EntryPoint(name="ghost", value="no.such.module:THING", group=PLUGIN_GROUP)]

    with pytest.raises(ReasonerPluginError, match="failed to load") as exc_info:
        discover_plugins(entry_points=eps)
    assert exc_info.value.plugin_name == "ghost"


# ---------------------------------------------------------------------------
# build_registry / allowlist_from_settings
# ---------------------------------------------------------------------------


def test_build_registry_registers_every_plugin():
    registry = build_registry([ALPHA, PLUGIN])

    assert registry.is_registered("alpha-reasoner")
    assert registry.is_registered("stub-reasoner")
    assert registry.get("stub-reasoner") is STUB_REGISTRATION


def test_allowlist_from_blank_setting_is_none():
    fw = FrameworkSettings(decisionledger_reasoners="")

    assert allowlist_from_settings(fw) is None


def test_allowlist_from_setting_splits_and_strips_ids():
    fw = FrameworkSettings(decisionledger_reasoners=" alpha-reasoner, stub-reasoner ,")

    assert allowlist_from_settings(fw) == frozenset({"alpha-reasoner", "stub-reasoner"})


# ---------------------------------------------------------------------------
# create_app — routes only; the lifespan is never started
# ---------------------------------------------------------------------------


def _route_paths(app) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def test_create_app_mounts_stub_router_next_to_framework_routes():
    """The 'second reasoner, zero framework diff' proof: no app/ edit needed."""
    app = create_app(plugins=[PLUGIN], gate_plugins=[])

    paths = _route_paths(app)
    assert "/api/v1/stub/ping" in paths
    assert "/health" in paths
    assert "/api/v1/decisions/{decision_id}" in paths
    assert "/api/v1/decisions/{decision_id}/replay" in paths


def test_create_app_mounts_every_plugin_router():
    app = create_app(plugins=[ALPHA, PLUGIN], gate_plugins=[])

    paths = _route_paths(app)
    assert "/api/v1/alpha/decisions" in paths
    assert "/api/v1/stub/ping" in paths


def test_create_app_with_no_plugins_keeps_framework_routes():
    app = create_app(plugins=[], gate_plugins=[])

    paths = _route_paths(app)
    assert "/health" in paths
    assert "/api/v1/decisions/{decision_id}" in paths
    assert not any(p.startswith("/api/v1/stub") for p in paths)


# ---------------------------------------------------------------------------
# create_app — lifespan, with the framework bootstrap replaced by a fake
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_framework(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``build_framework_services`` so no infrastructure is touched.

    Returns the ``FrameworkServices`` double the lifespan will receive; the
    gate plugins the lifespan handed to the bootstrap are recorded on its
    ``received_gate_plugins`` attribute.
    """
    framework = MagicMock(spec=FrameworkServices)
    framework.gates = {}

    def _build(settings: Any, plugins: Any, gate_plugins: Any) -> MagicMock:
        framework.received_gate_plugins = gate_plugins
        return framework

    monkeypatch.setattr("app.main.build_framework_services", _build)
    return framework


@pytest.fixture
def journal(fake_framework: MagicMock) -> list[str]:
    """Shared close-order journal wired into the fake for one test."""
    entries: list[str] = []
    fake_framework.close.side_effect = lambda: entries.append("framework")
    ALPHA.journal = entries
    ALPHA.built_with = None
    ALPHA.services = None
    PLUGIN.services = None
    yield entries
    ALPHA.journal = None


def test_lifespan_builds_each_plugin_against_the_framework_services(
    fake_framework: MagicMock, journal: list[str]
):
    app = create_app(plugins=[PLUGIN, ALPHA], gate_plugins=[])

    with TestClient(app) as client:
        assert app.state.framework is fake_framework
        assert set(app.state.reasoners) == {"alpha-reasoner", "stub-reasoner"}
        assert app.state.reasoners["stub-reasoner"] is PLUGIN.services
        assert app.state.reasoners["alpha-reasoner"] is ALPHA.services
        assert isinstance(PLUGIN.services, StubServices)
        assert PLUGIN.services.framework is fake_framework
        assert ALPHA.built_with is fake_framework
        assert client.get("/health").status_code == 200


def test_lifespan_serves_stub_routes_once_started(
    fake_framework: MagicMock, journal: list[str]
):
    with TestClient(create_app(plugins=[PLUGIN], gate_plugins=[])) as client:
        response = client.get("/api/v1/stub/ping")

    assert response.status_code == 200
    assert response.json() == {"reasoner_id": "stub-reasoner", "status": "ok"}


def test_shutdown_closes_reasoners_before_the_framework(
    fake_framework: MagicMock, journal: list[str]
):
    with TestClient(create_app(plugins=[PLUGIN, ALPHA], gate_plugins=[])):
        assert journal == []
        assert PLUGIN.services is not None
        assert not PLUGIN.services.closed

    assert PLUGIN.services.closed
    assert ALPHA.services is not None
    assert ALPHA.services.closed
    assert journal == ["alpha-reasoner", "framework"]
    fake_framework.close.assert_called_once()


def test_plugin_build_failure_closes_built_services_and_propagates(
    fake_framework: MagicMock, journal: list[str]
):
    exploding = _ExplodingPlugin("gamma-reasoner", "/api/v1/gamma")
    app = create_app(plugins=[ALPHA, exploding], gate_plugins=[])

    with (
        pytest.raises(RuntimeError, match="gamma-reasoner cannot start"),
        TestClient(app),
    ):
        pass

    assert ALPHA.services is not None
    assert ALPHA.services.closed
    assert journal == ["alpha-reasoner", "framework"]
    assert not hasattr(app.state, "reasoners")


def test_lifespan_with_no_plugins_warns_and_serves_framework_routes(
    fake_framework: MagicMock, caplog: pytest.LogCaptureFixture
):
    app = create_app(plugins=[], gate_plugins=[])

    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        assert app.state.reasoners == {}
        assert client.get("/health").status_code == 200

    assert any("api.no_reasoners" in rec.getMessage() for rec in caplog.records)
    fake_framework.close.assert_called_once()


def test_lifespan_hands_explicit_gate_plugins_to_the_bootstrap(
    fake_framework: MagicMock, journal: list[str]
):
    gate = FakeGatePlugin("fake")

    with TestClient(create_app(plugins=[PLUGIN], gate_plugins=[gate])):
        assert fake_framework.received_gate_plugins == [gate]


def test_create_app_without_gate_plugins_composes_every_installed_gate(
    fake_framework: MagicMock, journal: list[str], monkeypatch: pytest.MonkeyPatch
):
    discovered = [FakeGatePlugin("fake")]
    monkeypatch.setattr("app.main.discover_gates", lambda: discovered)

    with TestClient(create_app(plugins=[PLUGIN])):
        assert fake_framework.received_gate_plugins == discovered


# ---------------------------------------------------------------------------
# create_app() with no explicit plugins — the production composition path
# ---------------------------------------------------------------------------


def test_create_app_without_plugins_discovers_through_the_settings_allowlist(
    monkeypatch: pytest.MonkeyPatch,
):
    """``create_app()`` reads DECISIONLEDGER_REASONERS and hands it to discovery."""
    seen: list[frozenset[str] | None] = []

    def _recording_discover(*, allow: frozenset[str] | None = None):
        seen.append(allow)
        return [PLUGIN]

    monkeypatch.setattr("app.main.discover_plugins", _recording_discover)
    monkeypatch.setenv("DECISIONLEDGER_REASONERS", "stub-reasoner")

    app = create_app()

    assert seen == [frozenset({"stub-reasoner"})]
    assert "/api/v1/stub/ping" in _route_paths(app)


def test_create_app_without_plugins_passes_no_allowlist_when_setting_is_blank(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[frozenset[str] | None] = []

    def _recording_discover(*, allow: frozenset[str] | None = None):
        seen.append(allow)
        return []

    monkeypatch.setattr("app.main.discover_plugins", _recording_discover)
    monkeypatch.setenv("DECISIONLEDGER_REASONERS", "")

    app = create_app()

    assert seen == [None]
    assert "/health" in _route_paths(app)
