"""Behavioral tests for ``app.bootstrap.build_framework_services``.

``build_framework_services`` is the single place the framework wires its
shared services from a ``FrameworkSettings``, the composed reasoner plugins,
and the composed gate plugins. Every lifespan test replaces it with a fake,
so without this module the wiring itself — which plugin fields feed which
service, and how gates are built — has no unit coverage.

The Postgres connection and the DDL step are replaced with recording fakes
at the ``app.bootstrap`` module boundary, and gates are the scripted fakes
from ``tests/_fake_gate``. No Postgres, Elasticsearch, model download, or LLM
client is touched. What is asserted is the composition:

- the canonical DDL is applied to the framework's connection, and that
  connection is all the framework itself opens (DR-28: gates own their
  infrastructure);
- the bundle store receives one raw-event factory per ``reasoner_id`` and the
  registry built from the plugins' registrations;
- every gate plugin is built once against the services, ``FrameworkServices``
  exposes the composed gates read-only, and a gate sees the composed
  reasoners but never another gate;
- a gate that fails to build closes the gates built before it and the
  connection, and the error propagates; a malformed gate plugin fails
  bootstrap with ``GatePluginError``;
- ``close()`` closes the gates, then the connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from app import bootstrap
from app.audit.store import BundleStore
from app.gates import ComposedGate
from app.plugin import FrameworkServices
from app.settings import FrameworkSettings
from core.exceptions import GatePluginError
from tests._fake_gate.plugin import FakeGate, FakeGatePlugin
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.observation import ReasonerRegistry


class _RecordingStore(BundleStore):
    """``BundleStore`` that remembers the constructor arguments it received."""

    last_factories: Mapping[str, Any] | None = None
    last_registry: ReasonerRegistry | None = None

    def __init__(self, conn: Any, *, raw_event_factories: Any, registry: Any) -> None:
        super().__init__(
            conn, raw_event_factories=raw_event_factories, registry=registry
        )
        _RecordingStore.last_factories = raw_event_factories
        _RecordingStore.last_registry = registry


class _ExplodingGatePlugin(FakeGatePlugin):
    """A gate plugin whose build fails at startup."""

    def build(self, framework: FrameworkServices) -> FakeGate:
        raise RuntimeError(f"{self.gate_id} cannot start")


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the framework's Postgres connect and DDL step with fakes.

    Returns the fakes plus a shared ``calls`` journal recording what the
    bootstrap opened and applied, in order.
    """
    calls: list[str] = []
    pg_conn = MagicMock(name="pg_conn")
    pg_conn.close.side_effect = lambda: calls.append("pg_conn.close")

    def _connect(dsn: str) -> MagicMock:
        calls.append(f"connect:{dsn}")
        return pg_conn

    def _apply_schema(conn: Any) -> None:
        assert conn is pg_conn
        calls.append("apply_canonical_schema")

    monkeypatch.setattr(bootstrap.psycopg, "connect", _connect)
    monkeypatch.setattr(bootstrap, "apply_canonical_schema", _apply_schema)
    monkeypatch.setattr(bootstrap, "BundleStore", _RecordingStore)
    _RecordingStore.last_factories = None
    _RecordingStore.last_registry = None
    return {"calls": calls, "pg_conn": pg_conn}


@pytest.fixture
def settings() -> FrameworkSettings:
    return FrameworkSettings(
        postgres_host="db.example",
        postgres_port=5433,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        elasticsearch_url="http://es.example:9200",
        openai_api_key="",
    )


def test_bootstrap_returns_framework_services_bound_to_the_settings(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [PLUGIN], [])

    assert isinstance(services, FrameworkServices)
    assert services.settings is settings
    assert services.pg_conn is fakes["pg_conn"]
    assert isinstance(services.store, BundleStore)
    assert services.reasoners == (PLUGIN,)
    assert fakes["calls"][0] == f"connect:{settings.postgres_dsn}"


def test_bootstrap_opens_only_postgres_and_applies_the_canonical_ddl(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    bootstrap.build_framework_services(settings, [PLUGIN], [])

    assert fakes["calls"] == [
        f"connect:{settings.postgres_dsn}",
        "apply_canonical_schema",
    ]


def test_bootstrap_gives_the_store_one_raw_event_factory_per_reasoner(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    bootstrap.build_framework_services(settings, [PLUGIN], [])

    assert _RecordingStore.last_factories is not None
    assert set(_RecordingStore.last_factories) == {STUB_REGISTRATION.reasoner_id}
    assert (
        _RecordingStore.last_factories[STUB_REGISTRATION.reasoner_id]
        is PLUGIN.raw_event_factory
    )


def test_bootstrap_registry_holds_each_plugin_registration(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [PLUGIN], [])

    assert services.registry.get(STUB_REGISTRATION.reasoner_id) is STUB_REGISTRATION
    assert _RecordingStore.last_registry is services.registry


def test_bootstrap_with_no_plugins_builds_an_empty_registry_store_and_gate_set(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [], [])

    assert not services.registry.is_registered(STUB_REGISTRATION.reasoner_id)
    assert _RecordingStore.last_factories == {}
    assert dict(services.gates) == {}


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_bootstrap_builds_each_gate_plugin_once_and_exposes_the_composed_gates(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    fake = FakeGatePlugin("fake")
    rules = FakeGatePlugin("fake-rules", deterministic=True)

    services = bootstrap.build_framework_services(settings, [PLUGIN], [fake, rules])

    assert list(services.gates) == ["fake", "fake-rules"]
    assert len(fake.built) == 1
    assert len(rules.built) == 1
    composed = services.gates["fake"]
    assert isinstance(composed, ComposedGate)
    assert composed.plugin is fake
    assert composed.gate is fake.built[0]


def test_a_gate_is_built_against_the_reasoners_but_never_sees_another_gate(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    fake = FakeGatePlugin("fake")
    rules = FakeGatePlugin("fake-rules", deterministic=True)

    services = bootstrap.build_framework_services(settings, [PLUGIN], [fake, rules])

    built_against = rules.built[0].framework
    assert built_against.reasoners == (PLUGIN,)
    assert built_against.store is services.store
    assert dict(built_against.gates) == {}


def test_the_composed_gate_map_is_read_only(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(
        settings, [PLUGIN], [FakeGatePlugin("fake")]
    )

    with pytest.raises(TypeError):
        services.gates["other"] = services.gates["fake"]  # type: ignore[index]


def test_bootstrap_discovers_installed_gate_plugins_by_default(
    fakes: dict[str, Any], settings: FrameworkSettings, monkeypatch: pytest.MonkeyPatch
):
    discovered = FakeGatePlugin("fake")
    monkeypatch.setattr(bootstrap, "discover_gates", lambda: [discovered])

    services = bootstrap.build_framework_services(settings, [PLUGIN])

    assert list(services.gates) == ["fake"]


def test_a_gate_build_failure_closes_earlier_gates_and_the_connection(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    first = FakeGatePlugin("fake")
    exploding = _ExplodingGatePlugin("fake-rules", deterministic=True)

    with pytest.raises(RuntimeError, match="fake-rules cannot start"):
        bootstrap.build_framework_services(settings, [PLUGIN], [first, exploding])

    assert first.built[0].closed is True
    assert fakes["calls"][-1] == "pg_conn.close"


def test_a_malformed_gate_plugin_fails_bootstrap(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    unknown_kind = FakeGatePlugin("fake")
    unknown_kind.kind = "oracle"  # type: ignore[assignment]

    with pytest.raises(GatePluginError, match="unknown kind 'oracle'"):
        bootstrap.build_framework_services(settings, [PLUGIN], [unknown_kind])

    assert fakes["calls"][-1] == "pg_conn.close"


def test_two_gate_plugins_with_one_gate_id_fail_bootstrap(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    with pytest.raises(GatePluginError, match="share this gate_id"):
        bootstrap.build_framework_services(
            settings, [PLUGIN], [FakeGatePlugin("fake"), FakeGatePlugin("fake")]
        )


class _JournalingGatePlugin(FakeGatePlugin):
    """Builds fake gates whose close() lands in the bootstrap call journal."""

    def __init__(self, gate_id: str, calls: list[str], *, fail_close: bool = False):
        super().__init__(gate_id)
        self._calls = calls
        self._fail_close = fail_close

    def build(self, framework: FrameworkServices) -> FakeGate:
        gate = super().build(framework)
        calls, fail, gate_id = self._calls, self._fail_close, self.gate_id

        def _close() -> None:
            calls.append(f"gate.close:{gate_id}")
            if fail:
                raise RuntimeError(f"{gate_id} close failed")

        gate.close = _close  # type: ignore[method-assign]
        return gate


def test_close_closes_the_gates_in_reverse_order_then_the_connection(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    calls = fakes["calls"]
    gates = [_JournalingGatePlugin("a", calls), _JournalingGatePlugin("b", calls)]
    services = bootstrap.build_framework_services(settings, [PLUGIN], gates)
    calls.clear()

    services.close()

    assert calls == ["gate.close:b", "gate.close:a", "pg_conn.close"]


def test_close_releases_the_connection_even_when_a_gate_close_raises(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    calls = fakes["calls"]
    gates = [
        _JournalingGatePlugin("a", calls),
        _JournalingGatePlugin("b", calls, fail_close=True),
    ]
    services = bootstrap.build_framework_services(settings, [PLUGIN], gates)
    calls.clear()

    with pytest.raises(RuntimeError, match="b close failed"):
        services.close()

    assert calls == ["gate.close:b", "gate.close:a", "pg_conn.close"]
