"""Behavioral tests for the deployment's registration file (DR-31).

A reasoner that runs as its own service, with no plugin installed in the
ledger's deployment, registers through ``DECISIONLEDGER_REGISTRATIONS``: a
YAML list of registrations (or ``{registrations: [...]}``), each in the JSON
shape of a ``ReasonerRegistration`` and validated strictly.

- The file's registrations compose with the plugins', labelled with their
  source.
- A file that cannot be read or parsed, an entry that does not validate, a
  reasoner registered twice in the file, and a reasoner a plugin already
  registers each raise ``RegistrationError`` naming the file.
- The bootstrap reads the file named by the settings and snapshots every
  registration it serves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from app import bootstrap
from app.plugins import build_registry, load_registration_file
from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.exceptions import GatePluginError, RegistrationError
from core.gate import AllOf, Compare
from core.observation import (
    BypassRule,
    GateBinding,
    ReasonerRegistration,
    canonical_registration,
    registration_version,
)
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from pathlib import Path

_ENTRY = """\
  - reasoner_id: agent-reasoner
    reasoner_name: Agent Reasoner
    gates:
      - {gate_id: rules, mandatory: true}
      - {gate_id: policy}
    bypass_rules:
      - rule_id: confident-allow
        version: "2"
        description: the agent is confident
        predicate:
          kind: all
          predicates:
            - {kind: compare, field: label_value, op: lt, value: 0.1}
            - {kind: compare, field: feature_set.tool_calls, op: ge, value: 1}
        action: ALLOW
    recordable_actions: [ALLOW]
    novel_entity_feature: first_contact
    jurisdiction_conflict_groups: [[EU], [US, CA]]
"""

AGENT = ReasonerRegistration(
    reasoner_id="agent-reasoner",
    reasoner_name="Agent Reasoner",
    gates=(GateBinding(gate_id="rules", mandatory=True), GateBinding(gate_id="policy")),
    bypass_rules=(
        BypassRule(
            rule_id="confident-allow",
            version="2",
            description="the agent is confident",
            predicate=AllOf(
                predicates=[
                    Compare(field="label_value", op="lt", value=0.1),
                    Compare(field="feature_set.tool_calls", op="ge", value=1),
                ]
            ),
            action=DecisionAction.ALLOW,
        ),
    ),
    recordable_actions=frozenset({DecisionAction.ALLOW}),
    novel_entity_feature="first_contact",
    jurisdiction_conflict_groups=(frozenset({"EU"}), frozenset({"US", "CA"})),
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "registrations.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("prefix", ["", "registrations:\n"])
def test_a_registration_file_holds_a_list_of_registrations(tmp_path: Path, prefix):
    path = _write(tmp_path, prefix + _ENTRY)

    [registration] = load_registration_file(path)

    assert canonical_registration(registration) == canonical_registration(
        ReasonerRegistration.model_validate_json(AGENT.model_dump_json())
    )
    assert registration_version(registration) == registration_version(
        ReasonerRegistration.model_validate_json(AGENT.model_dump_json())
    )


def test_the_files_registrations_compose_with_the_plugins(tmp_path: Path):
    path = _write(tmp_path, _ENTRY)

    registry = build_registry([PLUGIN], registration_file=path)

    assert [(e.reasoner_id, e.source) for e in registry.entries()] == [
        ("agent-reasoner", f"file:{path}"),
        ("stub-reasoner", "plugin:tests._stub_reasoner.plugin"),
    ]
    assert registry.get("stub-reasoner") is STUB_REGISTRATION


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("registrations: [\n", "cannot be read as YAML"),
        ("reasoner_id: alone\n", "expected a list of registrations"),
        ("- {reasoner_id: x}\n", r"registration #0 is invalid: reasoner_name"),
        (
            "- {reasoner_id: x, reasoner_name: X, gates: [],\n"
            "   recordable_actions: [MAYBE]}\n",
            "registration #0 is invalid: recordable_actions",
        ),
        (_ENTRY + _ENTRY, "registered twice"),
    ],
)
def test_a_bad_registration_file_is_refused(tmp_path: Path, text: str, message: str):
    path = _write(tmp_path, text)
    with pytest.raises(RegistrationError, match=message) as err:
        load_registration_file(path)
    assert err.value.source == f"file:{path}"


def test_a_missing_registration_file_is_refused(tmp_path: Path):
    with pytest.raises(RegistrationError, match="cannot be read"):
        load_registration_file(tmp_path / "absent.yaml")


def test_the_file_cannot_register_a_reasoner_a_plugin_registers(tmp_path: Path):
    path = _write(
        tmp_path,
        f"- {STUB_REGISTRATION.model_dump_json()}\n",
    )
    with pytest.raises(RegistrationError, match="already registered by plugin:"):
        build_registry([PLUGIN], registration_file=path)


@pytest.fixture
def no_postgres(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the bootstrap's Postgres and snapshot writes; record snapshots."""
    recorded: list[str] = []
    pg_conn = MagicMock(name="pg_conn", autocommit=True)
    monkeypatch.setattr(bootstrap.psycopg, "connect", lambda dsn, **kw: pg_conn)
    monkeypatch.setattr(bootstrap, "apply_canonical_schema", lambda conn: None)
    monkeypatch.setattr(
        "app.audit.registrations.RegistrationSnapshots.record",
        lambda self, registration: recorded.append(registration.reasoner_id) or "v",
    )
    return recorded


def test_the_bootstrap_reads_the_file_and_snapshots_every_registration(
    tmp_path: Path, no_postgres: list[str]
):
    path = _write(tmp_path, _ENTRY.replace("mandatory: true", "mandatory: false"))
    settings = FrameworkSettings(decisionledger_registrations=str(path))

    services: Any = bootstrap.build_framework_services(settings, [PLUGIN], [])

    assert [e.reasoner_id for e in services.registry.entries()] == [
        "agent-reasoner",
        "stub-reasoner",
    ]
    assert no_postgres == ["agent-reasoner", "stub-reasoner"]
    assert services.journal is not None


def test_a_file_registration_whose_mandatory_gate_is_not_composed_fails_startup(
    tmp_path: Path, no_postgres: list[str]
):
    """The gate-set check covers every registration, not only the plugins'."""
    settings = FrameworkSettings(
        decisionledger_registrations=str(_write(tmp_path, _ENTRY))
    )
    with pytest.raises(GatePluginError, match="'agent-reasoner' binds this gate"):
        bootstrap.build_framework_services(settings, [PLUGIN], [])


class _ScopedGatePlugin:
    """A gate that serves only the reasoners it names (``ReasonerScope``)."""

    gate_id = "scoped"
    deterministic = True
    config_model = None

    def __init__(self, served: set[str]) -> None:
        from app.gates import GateKind

        self.kind = GateKind.RULES
        self.served = served

    def build(self, framework: Any) -> Any:
        served = self.served

        class _Gate:
            async def evaluate(self, request: Any) -> Any:
                raise AssertionError("not called at startup")

            def close(self) -> None:
                pass

            def serves(self, reasoner_id: str) -> bool:
                return reasoner_id in served

        return _Gate()


_SCOPED_ENTRY = """\
- reasoner_id: file-reasoner
  reasoner_name: File Reasoner
  gates: [{gate_id: scoped}]
"""


def test_a_gate_that_cannot_serve_a_bound_reasoner_fails_startup(
    tmp_path: Path, no_postgres: list[str]
):
    settings = FrameworkSettings(
        decisionledger_registrations=str(_write(tmp_path, _SCOPED_ENTRY))
    )
    with pytest.raises(GatePluginError, match="'file-reasoner' binds this gate"):
        bootstrap.build_framework_services(
            settings, [PLUGIN], [_ScopedGatePlugin({"stub-reasoner"})]
        )
    services: Any = bootstrap.build_framework_services(
        settings, [PLUGIN], [_ScopedGatePlugin({"file-reasoner"})]
    )
    assert "scoped" in services.gates
