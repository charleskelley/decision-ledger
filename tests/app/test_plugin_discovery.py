"""Behavioral tests for reasoner plugin discovery and the app factory.

A second reasoner must be composable with zero framework changes. The stub
reasoner under ``tests/_stub_reasoner`` is that second reasoner: it is
published here as an in-memory ``importlib.metadata.EntryPoint`` named
``stub-reasoner`` (value ``tests._stub_reasoner.plugin:PLUGIN``), exactly
the shape an installed package declares, and composed through
``create_app(plugins=[...])``. Since DR-31 a plugin publishes data only —
its registration and the assets its gates read — and the ledger runs none of
its code:

- Discovery loads the stub (and any other declared plugin), sorted by
  entry-point name.
- The allowlist filters by ``reasoner_id`` without loading skipped plugins.
- An entry-point name that differs from ``registration.reasoner_id``, a
  duplicate ``reasoner_id``, and an object that does not satisfy
  ``ReasonerPlugin`` each raise ``ReasonerPluginError``.
- ``create_app`` serves the framework's own routes only, whatever plugins it
  composes; ``create_app()`` with no argument runs discovery with the
  ``DECISIONLEDGER_REASONERS`` allowlist.
- Starting the lifespan (with ``build_framework_services`` replaced by a
  fake) exposes the framework services and the key ring on ``app.state``,
  keeps no reasoner services there, closes the framework on shutdown, and
  warns when no reasoner is registered or no key is configured. No
  infrastructure is touched.
- ``create_app`` hands explicit gate plugins to the bootstrap unchanged and,
  without them, composes every installed gate (``decisionledger.gates``).
"""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.main import create_app
from app.plugin import FrameworkServices, ReasonerPlugin
from app.plugins import (
    PLUGIN_GROUP,
    allowlist_from_settings,
    build_registry,
    discover_plugins,
)
from app.reasoner_registry import RegisteredReasoner, StaticReasonerRegistry
from app.settings import FrameworkSettings
from core.exceptions import ReasonerPluginError
from core.observation import GateBinding, ReasonerRegistration
from tests._fake_gate.plugin import FakeGatePlugin
from tests._memory_ledger import TEST_KEYRING
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from app.auth import KeyRing

_HERE = Path(__file__).resolve().parent
_MODULE = __name__

#: The entry point an installed stub package would declare.
STUB_EP = EntryPoint(
    name=STUB_REGISTRATION.reasoner_id,
    value="tests._stub_reasoner.plugin:PLUGIN",
    group=PLUGIN_GROUP,
)


# ---------------------------------------------------------------------------
# A second, data-only plugin
# ---------------------------------------------------------------------------


def _registration(reasoner_id: str) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=f"{reasoner_id} (fake)",
        gates=(GateBinding(gate_id="policy"),),
    )


class _FakePlugin:
    """A reasoner plugin: a registration and two asset directories."""

    def __init__(self, reasoner_id: str) -> None:
        self.registration = _registration(reasoner_id)
        self.prompts_dir = _HERE / "prompts"
        self.corpus_dir = _HERE / "corpus"


ALPHA = _FakePlugin("alpha-reasoner")
#: Same id as the stub, published under a different entry-point name/value.
STUB_TWIN = _FakePlugin(STUB_REGISTRATION.reasoner_id)


def _ep(name: str, attr: str) -> EntryPoint:
    return EntryPoint(name=name, value=f"{_MODULE}:{attr}", group=PLUGIN_GROUP)


# ---------------------------------------------------------------------------
# The stub reasoner satisfies the plugin contract
# ---------------------------------------------------------------------------


def test_stub_plugin_satisfies_the_reasoner_plugin_protocol():
    assert isinstance(PLUGIN, ReasonerPlugin)
    assert isinstance(ALPHA, ReasonerPlugin)


def test_a_plugin_publishes_data_only():
    """The framework never calls into a plugin: no router, no services."""
    for attribute in ("router", "build_services"):
        assert not hasattr(PLUGIN, attribute)


class _LeakyPlugin(_FakePlugin):
    """A plugin still carrying the pre-DR-31 surface: a router and services."""

    def __init__(self) -> None:
        super().__init__("leaky-reasoner")
        self.router = APIRouter(prefix="/api/v1/leak")
        self.built = False

        @self.router.get("/ping")
        def _ping() -> dict[str, str]:
            return {"leaked": "yes"}

    def build_services(self, framework: Any) -> Any:
        self.built = True
        raise AssertionError("the ledger must not build a reasoner's services")


def test_the_ledger_ignores_a_plugins_router_and_services(monkeypatch):
    plugin = _LeakyPlugin()
    framework = MagicMock(spec=FrameworkServices)
    framework.gates = {}
    framework.registry = StaticReasonerRegistry([plugin.registration])
    monkeypatch.setattr("app.main.build_framework_services", lambda s, p, g: framework)
    app = create_app(plugins=[plugin], gate_plugins=[], keyring=TEST_KEYRING)

    with TestClient(app) as client:
        assert client.get("/api/v1/leak/ping").status_code == 404
    assert not plugin.built
    assert "/api/v1/leak/ping" not in _route_paths(app)


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
    # plugin: no asset directories.
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


def test_build_registry_registers_every_plugin_with_its_source():
    registry = build_registry([ALPHA, PLUGIN])

    assert registry.is_registered("alpha-reasoner")
    assert registry.is_registered("stub-reasoner")
    assert registry.get("stub-reasoner") is STUB_REGISTRATION
    assert [e.source for e in registry.entries()] == [
        f"plugin:{_MODULE}",
        "plugin:tests._stub_reasoner.plugin",
    ]


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


#: Every route the ledger serves, whatever it composes.
FRAMEWORK_PATHS = {
    "/health",
    "/api/v1/decisions",
    "/api/v1/decisions:batch",
    "/api/v1/decisions/{decision_id}",
    "/api/v1/decisions/{decision_id}/replay",
    "/api/v1/decisions/{decision_id}/resolutions",
    "/api/v1/entities/{entity_id}/decisions",
    "/api/v1/identity/resolve",
    "/api/v1/reasoners",
    "/api/v1/reasoners/{reasoner_id}",
    "/api/v1/reasoners/{reasoner_id}/registrations/{version}",
    "/api/v1/gates",
}


@pytest.mark.parametrize("plugins", [[], [PLUGIN], [ALPHA, PLUGIN]])
def test_create_app_serves_the_framework_routes_only(plugins):
    """The 'second reasoner, zero framework diff' proof: no route of its own."""
    paths = _route_paths(create_app(plugins=plugins, gate_plugins=[]))

    assert paths >= FRAMEWORK_PATHS
    assert not {p for p in paths if p.startswith("/api/")} - FRAMEWORK_PATHS


# ---------------------------------------------------------------------------
# create_app — lifespan, with the framework bootstrap replaced by a fake
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_framework(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``build_framework_services`` so no infrastructure is touched.

    Returns the ``FrameworkServices`` double the lifespan will receive; the
    plugins and gate plugins the lifespan handed to the bootstrap are
    recorded on its ``received_plugins`` / ``received_gate_plugins``.
    """
    framework = MagicMock(spec=FrameworkServices)
    framework.gates = {}
    framework.registry = StaticReasonerRegistry([STUB_REGISTRATION])

    def _build(settings: Any, plugins: Any, gate_plugins: Any) -> MagicMock:
        framework.received_plugins = plugins
        framework.received_gate_plugins = gate_plugins
        return framework

    monkeypatch.setattr("app.main.build_framework_services", _build)
    return framework


def test_lifespan_exposes_the_framework_and_the_key_ring(fake_framework: MagicMock):
    app = create_app(plugins=[PLUGIN, ALPHA], gate_plugins=[], keyring=TEST_KEYRING)

    with TestClient(app) as client:
        assert app.state.framework is fake_framework
        assert app.state.keyring is TEST_KEYRING
        assert not hasattr(app.state, "reasoners")
        assert fake_framework.received_plugins == [PLUGIN, ALPHA]
        assert client.get("/health").status_code == 200

    fake_framework.close.assert_called_once()


def test_lifespan_reads_the_key_ring_from_the_settings(
    fake_framework: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DECISIONLEDGER_API_KEYS", "stub-reasoner:from-env")
    app = create_app(plugins=[PLUGIN], gate_plugins=[])

    with TestClient(app):
        ring: KeyRing = app.state.keyring
        principal = ring.principal("from-env")
        assert principal is not None
        assert principal.reasoner_id == "stub-reasoner"


def test_lifespan_warns_when_no_reasoner_or_no_key_is_configured(
    fake_framework: MagicMock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    fake_framework.registry = StaticReasonerRegistry([])
    for name in ("DECISIONLEDGER_API_KEYS", "DECISIONLEDGER_OPERATOR_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("DECISIONLEDGER_API_KEYS_FILE", "")
    app = create_app(plugins=[], gate_plugins=[])

    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/api/v1/reasoners")
        assert response.status_code == 401

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("api.no_reasoners" in m for m in messages)
    assert any("api.no_keys" in m for m in messages)
    fake_framework.close.assert_called_once()


def test_a_bootstrap_failure_propagates(monkeypatch: pytest.MonkeyPatch):
    def _explode(settings: Any, plugins: Any, gate_plugins: Any) -> None:
        raise RuntimeError("postgres is down")

    monkeypatch.setattr("app.main.build_framework_services", _explode)
    app = create_app(plugins=[PLUGIN], gate_plugins=[], keyring=TEST_KEYRING)

    with pytest.raises(RuntimeError, match="postgres is down"), TestClient(app):
        pass
    assert not hasattr(app.state, "framework")


def test_lifespan_hands_explicit_gate_plugins_to_the_bootstrap(
    fake_framework: MagicMock,
):
    gate = FakeGatePlugin("fake")

    with TestClient(
        create_app(plugins=[PLUGIN], gate_plugins=[gate], keyring=TEST_KEYRING)
    ):
        assert fake_framework.received_gate_plugins == [gate]


def test_create_app_without_gate_plugins_composes_every_installed_gate(
    fake_framework: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    discovered = [FakeGatePlugin("fake")]
    monkeypatch.setattr("app.main.discover_gates", lambda: discovered)

    with TestClient(create_app(plugins=[PLUGIN], keyring=TEST_KEYRING)):
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
    assert _route_paths(app) >= FRAMEWORK_PATHS


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


def test_registered_reasoner_entries_carry_their_version():
    [entry] = StaticReasonerRegistry([STUB_REGISTRATION]).entries()
    assert isinstance(entry, RegisteredReasoner)
    assert entry.reasoner_id == "stub-reasoner"
    assert entry.source == "inline"
    assert len(entry.version) == 16
