"""Behavioral tests for gate plugin discovery and composition (``app/gates.py``).

A gate must be composable with zero framework changes (DR-28). The fake gate
under ``tests/_fake_gate`` is published here as in-memory
``importlib.metadata.EntryPoint`` objects under ``decisionledger.gates`` —
exactly the shape an installed gate member declares:

- Discovery loads the declared plugins, sorted by entry-point name.
- A failed load, an object that does not satisfy ``GatePlugin``, a name that
  differs from ``gate_id``, an unknown ``kind``, a non-bool ``deterministic``,
  a ``config_model`` that is not a Pydantic model, and a duplicate
  ``gate_id`` each raise ``GatePluginError``.
- ``build_gates`` builds each plugin once, refuses a built object that is not
  a ``Gate``, and closes what it built when a later build fails.
- ``close_gates`` closes in reverse composition order.
- A plugin may leave out ``binding_config_model`` (DR-33); one it declares
  must be a Pydantic model. ``admit_binding`` validates a binding's
  ``config`` against it, then asks a ``BindingValidator`` gate; a gate with
  neither refuses a binding that carries a configuration.
- The installed deployment publishes the policy gate (interim, from the
  framework package) and the rules gate (its own member).
"""

from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import Any

import pytest
from pydantic import BaseModel

from app.gates import (
    GATE_GROUP,
    BindingValidator,
    ComposedGate,
    Gate,
    GateKind,
    GatePlugin,
    admit_binding,
    binding_config_model,
    build_gates,
    check_gate_plugin,
    check_gate_set,
    close_gates,
    discover_gates,
)
from core.exceptions import GateConfigError, GatePluginError
from core.observation import GateBinding
from tests._fake_gate.plugin import (
    DETERMINISTIC_PLUGIN,
    PLUGIN,
    FakeBindingConfig,
    FakeGate,
    FakeGatePlugin,
    FakeLookupGatePlugin,
)
from tests._stub_reasoner.registry import STUB_REGISTRATION

_MODULE = __name__


def _ep(name: str, attr: str, module: str = "tests._fake_gate.plugin") -> EntryPoint:
    return EntryPoint(name=name, value=f"{module}:{attr}", group=GATE_GROUP)


FAKE_EP = _ep("fake", "PLUGIN")
FAKE_RULES_EP = _ep("fake-rules", "DETERMINISTIC_PLUGIN")


# Module-level objects the malformed-plugin entry points below point at.
class _NotAPlugin:
    gate_id = "not-a-plugin"


NOT_A_PLUGIN = _NotAPlugin()

UNKNOWN_KIND = FakeGatePlugin("unknown-kind")
UNKNOWN_KIND.kind = "oracle"  # type: ignore[assignment]

NON_BOOL_DETERMINISTIC = FakeGatePlugin("non-bool")
NON_BOOL_DETERMINISTIC.deterministic = "yes"  # type: ignore[assignment]

BAD_CONFIG_MODEL = FakeGatePlugin("bad-config")
BAD_CONFIG_MODEL.config_model = dict  # type: ignore[assignment]

FAKE_TWIN = FakeGatePlugin("fake")


# ---------------------------------------------------------------------------
# discover_gates
# ---------------------------------------------------------------------------


def test_discovery_loads_declared_gates_sorted_by_name():
    plugins = discover_gates(entry_points=[FAKE_RULES_EP, FAKE_EP])

    assert plugins == [PLUGIN, DETERMINISTIC_PLUGIN]
    assert [p.gate_id for p in plugins] == ["fake", "fake-rules"]


def test_discovery_with_no_entry_points_composes_no_gates():
    assert discover_gates(entry_points=[]) == []


def test_discovery_rejects_an_entry_point_that_fails_to_load():
    broken = _ep("ghost", "DOES_NOT_EXIST")

    with pytest.raises(GatePluginError, match="failed to load") as exc_info:
        discover_gates(entry_points=[broken])

    assert exc_info.value.gate_id == "ghost"


def test_discovery_rejects_an_object_that_is_not_a_gate_plugin():
    with pytest.raises(GatePluginError, match="GatePlugin protocol"):
        discover_gates(entry_points=[_ep("not-a-plugin", "NOT_A_PLUGIN", _MODULE)])


def test_discovery_rejects_a_name_that_differs_from_the_gate_id():
    with pytest.raises(GatePluginError, match="must equal gate_id"):
        discover_gates(entry_points=[_ep("renamed", "PLUGIN")])


@pytest.mark.parametrize(
    ("name", "attr", "fragment"),
    [
        ("unknown-kind", "UNKNOWN_KIND", "unknown kind 'oracle'"),
        ("non-bool", "NON_BOOL_DETERMINISTIC", "deterministic must be a bool"),
        ("bad-config", "BAD_CONFIG_MODEL", "config_model must be"),
    ],
)
def test_discovery_rejects_malformed_plugin_metadata(name, attr, fragment):
    with pytest.raises(GatePluginError, match=fragment):
        discover_gates(entry_points=[_ep(name, attr, _MODULE)])


def test_discovery_rejects_two_plugins_with_one_gate_id():
    with pytest.raises(GatePluginError, match="duplicate gate_id 'fake'"):
        discover_gates(entry_points=[FAKE_EP, _ep("fake", "FAKE_TWIN", _MODULE)])


def test_the_installed_deployment_publishes_the_policy_and_rules_gates():
    plugins = {p.gate_id: p for p in discover_gates()}

    assert set(plugins) == {"policy", "rules"}
    assert plugins["policy"].kind == GateKind.LLM
    assert plugins["policy"].deterministic is False
    assert plugins["rules"].kind == GateKind.RULES
    assert plugins["rules"].deterministic is True
    assert plugins["rules"].config_model is not None


# ---------------------------------------------------------------------------
# check_gate_plugin
# ---------------------------------------------------------------------------


def test_the_fake_gate_plugins_satisfy_the_protocol():
    assert isinstance(PLUGIN, GatePlugin)
    assert check_gate_plugin(PLUGIN, name="fake") is PLUGIN
    assert check_gate_plugin(DETERMINISTIC_PLUGIN, name="fake-rules")


def test_a_plugin_without_a_config_model_is_well_formed():
    plugin = FakeGatePlugin("no-config")
    plugin.config_model = None

    assert check_gate_plugin(plugin, name="no-config") is plugin


def test_a_pydantic_config_model_is_accepted():
    class _Config(BaseModel):
        threshold: float

    plugin = FakeGatePlugin("typed-config")
    plugin.config_model = _Config

    assert check_gate_plugin(plugin, name="typed-config") is plugin


def test_a_plugin_may_leave_out_the_binding_config_model():
    """It is optional: a plugin written before DR-33 stays well formed."""
    plugin = FakeLookupGatePlugin()

    assert not hasattr(plugin, "binding_config_model")
    assert check_gate_plugin(plugin, name="fake-lookup") is plugin
    assert binding_config_model(plugin) is None


def test_a_binding_config_model_that_is_not_a_pydantic_model_is_refused():
    plugin = FakeGatePlugin("bad-binding")
    plugin.binding_config_model = dict  # type: ignore[assignment]

    with pytest.raises(GatePluginError, match="binding_config_model must be"):
        check_gate_plugin(plugin, name="bad-binding")


# ---------------------------------------------------------------------------
# admit_binding (DR-33)
# ---------------------------------------------------------------------------


def _composed(plugin: Any) -> ComposedGate:
    return ComposedGate(plugin=plugin, gate=plugin.build(None))


def test_admit_binding_hands_the_gate_its_typed_configuration():
    composed = _composed(FakeGatePlugin())
    gate = composed.gate
    assert isinstance(gate, BindingValidator)

    admit_binding(composed, GateBinding(gate_id="fake", config={"label": "x"}), "r")

    assert gate.bindings == [("r", FakeBindingConfig(label="x"))]


def test_admit_binding_refuses_a_configuration_the_model_does_not_validate():
    composed = _composed(FakeGatePlugin())

    with pytest.raises(GateConfigError, match="config is invalid: nope"):
        admit_binding(composed, GateBinding(gate_id="fake", config={"nope": 1}), "r")
    assert composed.gate.bindings == []


def test_admit_binding_relays_the_gates_refusal():
    composed = _composed(FakeGatePlugin())

    with pytest.raises(GateConfigError, match="scripted to refuse"):
        admit_binding(
            composed, GateBinding(gate_id="fake", config={"refuse": True}), "r"
        )


def test_a_gate_that_takes_no_binding_configuration_admits_an_empty_one():
    composed = _composed(FakeLookupGatePlugin())

    admit_binding(composed, GateBinding(gate_id="fake-lookup"), "r")

    with pytest.raises(GateConfigError, match="takes no binding configuration"):
        admit_binding(
            composed, GateBinding(gate_id="fake-lookup", config={"k": 1}), "r"
        )


def test_a_validating_gate_without_a_model_gets_the_raw_configuration():
    plugin = FakeGatePlugin("raw")
    plugin.binding_config_model = None
    composed = _composed(plugin)

    admit_binding(composed, GateBinding(gate_id="raw", config={"k": [1]}), "r")

    assert composed.gate.bindings == [("r", {"k": [1]})]


class _NeedsAName(BaseModel):
    """A binding configuration that is never empty."""

    name: str


def test_a_mandated_gate_needing_binding_configuration_refuses_an_unbound_reasoner():
    # The gate reads its binding from every request, and a reasoner that does
    # not bind a mandated gate runs it with an empty binding configuration:
    # a gate whose binding model refuses {} cannot be mandated for it.
    plugin = FakeGatePlugin("named")
    plugin.binding_config_model = _NeedsAName
    composed = ComposedGate(plugin=plugin, gate=plugin.build(None), mandatory=True)
    registration = STUB_REGISTRATION.model_copy(update={"gates": ()})

    with pytest.raises(GatePluginError) as err:
        check_gate_set(registration, {"named": composed})

    assert err.value.gate_id == "named"
    assert "refuses the empty configuration it would run with" in err.value.reason
    assert composed.gate.bindings == []


def test_a_mandated_gate_that_accepts_an_empty_binding_serves_an_unbound_reasoner():
    plugin = FakeGatePlugin("optional")
    composed = ComposedGate(plugin=plugin, gate=plugin.build(None), mandatory=True)

    check_gate_set(
        STUB_REGISTRATION.model_copy(update={"gates": ()}), {"optional": composed}
    )


# ---------------------------------------------------------------------------
# build_gates / close_gates
# ---------------------------------------------------------------------------


class _NotAGate:
    """Built object missing ``evaluate`` — not a ``Gate``."""

    def close(self) -> None:
        pass


class _BuildsJunk(FakeGatePlugin):
    def build(self, framework: Any) -> Any:
        return _NotAGate()


class _Explodes(FakeGatePlugin):
    def build(self, framework: Any) -> FakeGate:
        raise RuntimeError(f"{self.gate_id} cannot start")


class _JournalingGate(FakeGate):
    journal: list[str]

    def close(self) -> None:
        super().close()
        self.journal.append(self.gate_id)


class _JournalingPlugin(FakeGatePlugin):
    def __init__(self, gate_id: str, journal: list[str]) -> None:
        super().__init__(gate_id)
        self._journal = journal

    def build(self, framework: Any) -> FakeGate:
        gate = _JournalingGate(
            gate_id=self.gate_id, deterministic=False, framework=framework
        )
        gate.journal = self._journal
        self.built.append(gate)
        return gate


def test_build_gates_builds_each_plugin_once_against_the_framework():
    framework = object()
    first, second = FakeGatePlugin("fake"), FakeGatePlugin("other")

    composed = build_gates([first, second], framework)  # type: ignore[arg-type]

    assert list(composed) == ["fake", "other"]
    assert composed["fake"] == ComposedGate(plugin=first, gate=first.built[0])
    assert composed["fake"].gate_id == "fake"
    assert first.built[0].framework is framework
    assert isinstance(composed["other"].gate, Gate)


def test_build_gates_rejects_a_built_object_that_is_not_a_gate():
    with pytest.raises(GatePluginError, match="Gate protocol"):
        build_gates([_BuildsJunk("junk")], object())  # type: ignore[arg-type]


def test_build_gates_rejects_two_plugins_with_one_gate_id():
    with pytest.raises(GatePluginError, match="share this gate_id"):
        build_gates([FakeGatePlugin("fake"), FakeGatePlugin("fake")], object())  # type: ignore[arg-type]


def test_build_gates_closes_what_it_built_when_a_later_build_fails():
    first = FakeGatePlugin("fake")

    with pytest.raises(RuntimeError, match="boom cannot start"):
        build_gates([first, _Explodes("boom")], object())  # type: ignore[arg-type]

    assert first.built[0].closed is True


def test_close_gates_closes_in_reverse_composition_order():
    journal: list[str] = []
    composed = build_gates(
        [_JournalingPlugin("a", journal), _JournalingPlugin("b", journal)],
        object(),  # type: ignore[arg-type]
    )

    close_gates(composed)

    assert journal == ["b", "a"]


class _SyncGate:
    """A gate whose evaluate is not a coroutine function."""

    def __init__(self) -> None:
        self.closed = False

    def evaluate(self, request: Any) -> Any:
        return None

    def close(self) -> None:
        self.closed = True


class _BuildsSyncGate(FakeGatePlugin):
    def __init__(self, gate_id: str) -> None:
        super().__init__(gate_id)
        self.sync_gate = _SyncGate()

    def build(self, framework: Any) -> Any:
        return self.sync_gate


def test_build_gates_rejects_a_synchronous_evaluate_at_startup_and_closes_it():
    plugin = _BuildsSyncGate("sync")

    with pytest.raises(GatePluginError, match="synchronous evaluate"):
        build_gates([plugin], object())  # type: ignore[arg-type]

    assert plugin.sync_gate.closed is True


class _FailsToClose(FakeGate):
    def close(self) -> None:
        super().close()
        raise RuntimeError(f"{self.gate_id} close failed")


class _BuildsFailingCloser(FakeGatePlugin):
    def build(self, framework: Any) -> FakeGate:
        gate = _FailsToClose(
            gate_id=self.gate_id, deterministic=False, framework=framework
        )
        self.built.append(gate)
        return gate


def test_close_gates_closes_every_gate_even_when_one_raises():
    first, failing, last = (
        FakeGatePlugin("a"),
        _BuildsFailingCloser("b"),
        FakeGatePlugin("c"),
    )
    composed = build_gates([first, failing, last], object())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="b close failed"):
        close_gates(composed)

    assert first.built[0].closed is True
    assert failing.built[0].closed is True
    assert last.built[0].closed is True
