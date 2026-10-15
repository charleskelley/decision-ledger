"""Behavioral tests for the registrations and gates resources (read-only).

- ``GET /api/v1/reasoners`` lists every registration the deployment serves
  — installed plugins and the registration file — with its version (the
  content address bundles record) and its source.
- ``GET /api/v1/reasoners/{id}`` serves one; an unknown id is 404.
- ``GET /api/v1/reasoners/{id}/registrations/{version}`` serves any stored
  version — the one a bundle's ``registration_version`` names.
- ``GET /api/v1/gates`` lists the composed gates with kind, determinism,
  the deployment's mandate, and the JSON schema of their ``gate_config``.
- Every resource needs a key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.observation import canonical_registration, registration_version
from tests._fake_gate.plugin import FakeGatePlugin, FakeLookupGatePlugin
from tests._memory_ledger import OPERATOR_KEY, memory_framework
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION
from tests.app.conftest import start_ledger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.app.conftest import Ledger

_FILE_REGISTRATION = """\
registrations:
  - reasoner_id: file-reasoner
    reasoner_name: File Reasoner
    gates:
      - {gate_id: fake-lookup, mandatory: true}
    bypass_rules:
      - rule_id: always
        version: "1"
        description: everything may be recorded
        predicate: {kind: compare, field: label_value, op: ge, value: 0}
        action: ALLOW
    recordable_actions: [ALLOW]
"""


@pytest.fixture
def two_source_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Ledger]:
    path = tmp_path / "registrations.yaml"
    path.write_text(_FILE_REGISTRATION, encoding="utf-8")
    fake = FakeGatePlugin()
    framework = memory_framework(
        [PLUGIN],
        [fake, FakeLookupGatePlugin()],
        mandatory=["fake-lookup"],
        registration_file=path,
    )
    yield from start_ledger(monkeypatch, framework, fake)


def test_the_registrations_resource_lists_every_source(two_source_ledger: Ledger):
    response = two_source_ledger.get("/api/v1/reasoners")

    assert response.status_code == 200
    reasoners = {r["reasoner_id"]: r for r in response.json()["reasoners"]}
    assert sorted(reasoners) == ["file-reasoner", "stub-reasoner"]
    stub = reasoners["stub-reasoner"]
    assert stub["source"] == "plugin:tests._stub_reasoner.plugin"
    assert stub["registration_version"] == registration_version(STUB_REGISTRATION)
    assert stub["registration"] == canonical_registration(STUB_REGISTRATION)
    assert stub["reasoner_name"] == "Stub Reasoner"
    assert reasoners["file-reasoner"]["source"].startswith("file:")
    assert reasoners["file-reasoner"]["registration"]["recordable_actions"] == ["ALLOW"]


def test_one_registration_and_any_stored_version_are_served(ledger: Ledger):
    version = registration_version(STUB_REGISTRATION)

    current = ledger.get("/api/v1/reasoners/stub-reasoner").json()
    assert current["registration_version"] == version

    stored = ledger.get(f"/api/v1/reasoners/stub-reasoner/registrations/{version}")
    assert stored.status_code == 200
    body = stored.json()
    assert body["registration_version"] == version
    assert body["registration"] == canonical_registration(STUB_REGISTRATION)
    assert body["first_registered_at"]


def test_unknown_reasoners_and_versions_are_404(ledger: Ledger):
    response = ledger.get("/api/v1/reasoners/nobody")
    assert (response.status_code, response.json()["code"]) == (
        404,
        "REASONER_NOT_FOUND",
    )
    response = ledger.get("/api/v1/reasoners/stub-reasoner/registrations/0000")
    assert (response.status_code, response.json()["code"]) == (
        404,
        "REGISTRATION_VERSION_NOT_FOUND",
    )


def test_the_gates_resource_describes_every_composed_gate(two_source_ledger: Ledger):
    response = two_source_ledger.get("/api/v1/gates", key=OPERATOR_KEY)

    assert response.status_code == 200
    gates = {g["gate_id"]: g for g in response.json()["gates"]}
    assert sorted(gates) == ["fake", "fake-lookup"]
    assert gates["fake"]["kind"] == "llm"
    assert gates["fake"]["deterministic"] is False
    assert gates["fake"]["mandatory"] is False
    assert "permitted_actions" in gates["fake"]["config_schema"]["properties"]
    assert gates["fake-lookup"]["kind"] == "lookup"
    assert gates["fake-lookup"]["mandatory"] is True


def test_every_resource_needs_a_key(ledger: Ledger):
    for path in (
        "/api/v1/reasoners",
        "/api/v1/reasoners/stub-reasoner",
        "/api/v1/gates",
    ):
        response = ledger.get(path, key=None)
        assert response.status_code == 401, path
        assert response.json()["code"] == "UNAUTHENTICATED"
