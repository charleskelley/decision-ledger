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
- the bundle store receives the connection and the registry built from the
  plugins' registrations, every registration the registry serves is
  snapshotted before the first decision (so every bundle's
  ``registration_version`` has one), and the resolution journal shares the
  connection;
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
from tests._fake_gate.plugin import FakeGate, FakeGatePlugin, FakeLookupGatePlugin
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from core.observation import ReasonerRegistry


class _RecordingStore(BundleStore):
    """``BundleStore`` that remembers the constructor arguments it received."""

    last_conn: Any = None
    last_registry: ReasonerRegistry | None = None
    last_artifacts: Any = None

    def __init__(self, conn: Any, *, registry: Any, artifacts: Any = None) -> None:
        super().__init__(conn, registry=registry, artifacts=artifacts)
        _RecordingStore.last_conn = conn
        _RecordingStore.last_registry = registry
        _RecordingStore.last_artifacts = artifacts


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

    def _connect(dsn: str, *, autocommit: bool = False) -> MagicMock:
        calls.append(f"connect:{dsn}")
        calls.append(f"autocommit:{autocommit}")
        return pg_conn

    def _apply_schema(conn: Any) -> None:
        assert conn is pg_conn
        calls.append("apply_canonical_schema")

    monkeypatch.setattr(bootstrap.psycopg, "connect", _connect)
    monkeypatch.setattr(bootstrap, "apply_canonical_schema", _apply_schema)
    monkeypatch.setattr(bootstrap, "BundleStore", _RecordingStore)
    _RecordingStore.last_conn = None
    _RecordingStore.last_registry = None
    _RecordingStore.last_artifacts = None
    return {"calls": calls, "pg_conn": pg_conn}


@pytest.fixture
def settings() -> FrameworkSettings:
    return FrameworkSettings(
        postgres_host="db.example",
        postgres_port=5433,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
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
        "autocommit:True",
        "apply_canonical_schema",
    ]


def test_the_store_gets_the_autocommit_connection_and_needs_no_reasoner_code(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    # Replay runs from the bundle: the store takes the connection and the
    # registry, and nothing from a reasoner plugin.
    bootstrap.build_framework_services(settings, [PLUGIN], [])
    assert _RecordingStore.last_conn is fakes["pg_conn"]
    assert not hasattr(PLUGIN, "raw_event_factory")


@pytest.mark.parametrize(
    ("setting", "escrows"), [("on", True), ("off", False), (" OFF ", False)]
)
def test_the_store_escrows_unless_the_deployment_turns_escrow_off(
    fakes: dict[str, Any], setting: str, escrows: bool
):
    services = bootstrap.build_framework_services(
        FrameworkSettings(decisionledger_evidence_escrow=setting), [PLUGIN], []
    )

    assert services.store.artifacts is _RecordingStore.last_artifacts
    assert services.store.artifacts.enabled is escrows


@pytest.mark.parametrize("setting", ["", "true", "1", "enabled", "of"])
def test_an_escrow_setting_neither_on_nor_off_refuses_startup_before_postgres(
    fakes: dict[str, Any], setting: str
):
    with pytest.raises(ValueError, match="DECISIONLEDGER_EVIDENCE_ESCROW"):
        bootstrap.build_framework_services(
            FrameworkSettings(decisionledger_evidence_escrow=setting), [PLUGIN], []
        )
    assert fakes["calls"] == []


def test_the_deployments_mandatory_gates_are_composed_flagged(fakes: dict[str, Any]):
    settings = FrameworkSettings(
        _env_file=None,  # type: ignore[call-arg]
        decisionledger_mandatory_gates=" fake-lookup , ",
    )
    services = bootstrap.build_framework_services(
        settings, [PLUGIN], [FakeGatePlugin(), FakeLookupGatePlugin()]
    )
    assert {g: c.mandatory for g, c in services.gates.items()} == {
        "fake": False,
        "fake-lookup": True,
    }


def test_a_mandated_gate_the_deployment_does_not_compose_fails_startup(
    fakes: dict[str, Any],
):
    settings = FrameworkSettings(
        _env_file=None,  # type: ignore[call-arg]
        decisionledger_mandatory_gates="missing",
    )
    with pytest.raises(GatePluginError, match="does not compose"):
        bootstrap.build_framework_services(settings, [PLUGIN], [FakeGatePlugin()])
    assert fakes["calls"][-1] == "pg_conn.close"


def test_bootstrap_registry_holds_each_plugin_registration(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [PLUGIN], [])

    assert services.registry.get(STUB_REGISTRATION.reasoner_id) is STUB_REGISTRATION
    assert _RecordingStore.last_registry is services.registry


def test_bootstrap_snapshots_every_registration_before_serving(
    fakes: dict[str, Any], settings: FrameworkSettings, monkeypatch: pytest.MonkeyPatch
):
    recorded: list[str] = []
    monkeypatch.setattr(
        "app.audit.registrations.RegistrationSnapshots.record",
        lambda self, registration: recorded.append(registration.reasoner_id) or "v",
    )

    services = bootstrap.build_framework_services(settings, [PLUGIN], [])

    assert recorded == [STUB_REGISTRATION.reasoner_id]
    assert services.journal is not None


def test_bootstrap_with_no_plugins_builds_an_empty_registry_store_and_gate_set(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [], [])

    assert not services.registry.is_registered(STUB_REGISTRATION.reasoner_id)
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


# ---------------------------------------------------------------------------
# The identity step (DR-32)
# ---------------------------------------------------------------------------


def _resolving_settings(resolver: str) -> FrameworkSettings:
    return FrameworkSettings(decisionledger_identity_resolver=resolver)


def test_bootstrap_keeps_the_passthrough_unless_a_resolver_is_named(
    fakes: dict[str, Any],
):
    from tests._stub_resolver.plugin import StubResolverPlugin

    plugin = StubResolverPlugin()
    services = bootstrap.build_framework_services(
        FrameworkSettings(), [PLUGIN], [], resolver_plugins=[plugin]
    )
    assert services.identity.resolver_id == "passthrough"
    assert plugin.built == []


def test_bootstrap_builds_the_named_resolver_against_the_services(
    fakes: dict[str, Any],
):
    from tests._stub_resolver.plugin import StubResolverPlugin

    plugin = StubResolverPlugin()
    services = bootstrap.build_framework_services(
        _resolving_settings("stub-resolver"), [PLUGIN], [], resolver_plugins=[plugin]
    )
    assert services.identity.resolver_id == "stub-resolver"
    assert services.identity.relationship_types == frozenset({"fleet"})
    [resolver] = plugin.built
    services.close()
    assert resolver.closed
    assert fakes["calls"][-1] == "pg_conn.close"


def test_a_named_resolver_that_is_not_installed_refuses_startup_before_postgres(
    fakes: dict[str, Any],
):
    from core.exceptions import ResolverPluginError

    with pytest.raises(ResolverPluginError, match="crosswalk"):
        bootstrap.build_framework_services(
            _resolving_settings("crosswalk"), [PLUGIN], [], resolver_plugins=[]
        )
    assert fakes["calls"] == []


def test_a_resolver_that_fails_to_build_closes_the_connection(fakes: dict[str, Any]):
    from tests._stub_resolver.plugin import StubResolverPlugin

    class _Exploding(StubResolverPlugin):
        def build(self, framework):  # type: ignore[override]
            raise RuntimeError("crosswalk artifact missing")

    with pytest.raises(RuntimeError, match="crosswalk artifact missing"):
        bootstrap.build_framework_services(
            _resolving_settings("stub-resolver"),
            [PLUGIN],
            [],
            resolver_plugins=[_Exploding()],
        )
    assert fakes["calls"][-1] == "pg_conn.close"


def test_a_gate_that_fails_to_build_closes_the_resolver_too(fakes: dict[str, Any]):
    from tests._stub_resolver.plugin import StubResolverPlugin

    plugin = StubResolverPlugin()
    with pytest.raises(RuntimeError, match="cannot start"):
        bootstrap.build_framework_services(
            _resolving_settings("stub-resolver"),
            [PLUGIN],
            [_ExplodingGatePlugin()],
            resolver_plugins=[plugin],
        )
    [resolver] = plugin.built
    assert resolver.closed
    assert fakes["calls"][-1] == "pg_conn.close"


def test_two_registrations_declaring_one_namespace_differently_refuse_startup(
    fakes: dict[str, Any],
):
    import uuid
    from dataclasses import dataclass

    from core.exceptions import RegistrationError
    from core.identity import NativeNamespace
    from core.observation import ReasonerRegistration

    @dataclass
    class _Plugin:
        registration: ReasonerRegistration
        prompts_dir: Any = PLUGIN.prompts_dir
        corpus_dir: Any = PLUGIN.corpus_dir

    def _declaring(reasoner_id: str) -> _Plugin:
        fields = {
            name: getattr(STUB_REGISTRATION, name)
            for name in ReasonerRegistration.model_fields
        }
        namespace = NativeNamespace(
            native_namespace="widget-db",
            namespace_uuid=uuid.uuid4(),
            entity_type="widget",
        )
        return _Plugin(
            ReasonerRegistration(
                **{
                    **fields,
                    "reasoner_id": reasoner_id,
                    "native_namespaces": (namespace,),
                }
            )
        )

    with pytest.raises(RegistrationError, match="declared differently"):
        bootstrap.build_framework_services(
            FrameworkSettings(), [_declaring("one"), _declaring("two")], []
        )
    assert fakes["calls"][-1] == "pg_conn.close"
