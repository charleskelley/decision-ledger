"""Behavioral tests for the registrations and gates resources.

- ``GET /api/v1/reasoners`` lists every registration the deployment serves
  — installed plugins, the registration file, and the registrations written
  over the API — with its version (the content address bundles record) and
  its source.
- ``GET /api/v1/reasoners/{id}`` serves one; an unknown id is 404.
- ``GET /api/v1/reasoners/{id}/registrations/{version}`` serves any stored
  version — the one a bundle's ``registration_version`` names.
- ``PUT /api/v1/reasoners/{id}`` (DR-33, operator key) creates a reasoner's
  registration (201, ``source: api``, with who and when) or replaces its API
  registration with a new version (200; the old version stays readable and
  replayable); an unchanged registration is 200 with the same version. The
  write is admitted as a registration is at startup, and a refusal stores
  nothing: a body that is not a registration for the path's reasoner, or
  holds what the ledger cannot store, is 422 ``REGISTRATION_INVALID``; a
  native namespace another registration declares differently is 422
  ``NATIVE_NAMESPACE_CONFLICT``; a gate set the composed gates cannot serve
  (a mandatory gate not composed, a binding a gate refuses, a configuration
  for a gate that takes none, a mandated gate that refuses the empty
  configuration) is 422 ``GATE_BINDING_REFUSED`` naming the gate. A
  plugin's or the file's reasoner is 409 ``REGISTRATION_SOURCE_IMMUTABLE``;
  a reasoner key is 403 ``OPERATOR_REQUIRED``. A reasoner registered over
  the API decides, its decisions replay against the version they cite, and
  its registration is served again after a restart.
- ``GET /api/v1/gates`` lists the composed gates with kind, determinism,
  the deployment's mandate, and the JSON schema of their ``gate_config``.
- Every resource needs a key.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import pytest

from core.identity import NativeNamespace
from core.observation import (
    GateBinding,
    GateContext,
    ReasonerRegistration,
    canonical_registration,
    registration_version,
)
from tests._fake_gate.plugin import (
    FakeBindingConfig,
    FakeGatePlugin,
    FakeLookupGatePlugin,
)
from tests._memory_ledger import (
    OPERATOR_KEY,
    OTHER_KEY,
    MemoryApiRegistrations,
    memory_framework,
)
from tests._stub_reasoner.assembler import build_submission
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION
from tests.app.conftest import STUB_KEY, start_ledger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from core.observation import ReasonerContext
    from tests._stub_reasoner.events import StubEvent
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
    response = ledger.put("/api/v1/reasoners/other-reasoner", "{}", key=None)
    assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")


# ---------------------------------------------------------------------------
# PUT /api/v1/reasoners/{reasoner_id} (DR-33)
# ---------------------------------------------------------------------------

#: The stub's registration, as another reasoner (the key ring holds a key for
#: ``other-reasoner``, so it can decide once it is registered).
OTHER = STUB_REGISTRATION.model_copy(
    update={"reasoner_id": "other-reasoner", "reasoner_name": "Other Reasoner"}
)


def _body(registration: ReasonerRegistration) -> str:
    return json.dumps(canonical_registration(registration))


def _put(
    ledger: Ledger,
    registration: ReasonerRegistration,
    *,
    key: str | None = OPERATOR_KEY,
):
    return ledger.put(
        f"/api/v1/reasoners/{registration.reasoner_id}", _body(registration), key=key
    )


def _problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == code
    return body


def test_the_operator_registers_a_reasoner_over_http(ledger: Ledger):
    response = _put(ledger, OTHER)

    assert response.status_code == 201, response.text
    assert response.headers["location"] == "/api/v1/reasoners/other-reasoner"
    body = response.json()
    assert body["reasoner_id"] == "other-reasoner"
    assert body["source"] == "api"
    assert body["registered_by"] == "operator"
    assert body["registered_at"]
    assert body["updated_at"] == body["registered_at"]
    assert body["registration_version"] == registration_version(OTHER)
    assert body["registration"] == canonical_registration(OTHER)
    served = ledger.get("/api/v1/reasoners/other-reasoner").json()
    assert served == body
    stored = ledger.get(
        f"/api/v1/reasoners/other-reasoner/registrations/{registration_version(OTHER)}"
    )
    assert stored.status_code == 200
    assert stored.json()["registration"] == canonical_registration(OTHER)


def test_declared_registrations_carry_no_registration_details(ledger: Ledger):
    body = ledger.get("/api/v1/reasoners/stub-reasoner").json()
    assert body["source"] == "plugin:tests._stub_reasoner.plugin"
    assert body["registered_by"] is None
    assert body["registered_at"] is None
    assert body["updated_at"] is None


def test_an_unchanged_registration_is_not_a_new_version(ledger: Ledger):
    first = _put(ledger, OTHER).json()

    again = _put(ledger, OTHER)

    assert again.status_code == 200
    assert "location" not in again.headers
    assert again.json() == first


def test_a_changed_registration_is_a_new_version_and_the_old_one_stays_served(
    ledger: Ledger,
):
    first = _put(ledger, OTHER).json()
    renamed = OTHER.model_copy(update={"reasoner_name": "Other, renamed"})

    response = _put(ledger, renamed)

    assert response.status_code == 200
    body = response.json()
    assert body["registration_version"] == registration_version(renamed)
    assert body["registration_version"] != first["registration_version"]
    assert body["registered_at"] == first["registered_at"]
    assert body["updated_at"] >= first["updated_at"]
    old = ledger.get(
        "/api/v1/reasoners/other-reasoner/registrations/"
        f"{first['registration_version']}"
    )
    assert old.status_code == 200
    assert old.json()["registration"]["reasoner_name"] == "Other Reasoner"


def test_a_reasoner_registered_over_http_decides_and_replays_against_its_version(
    ledger: Ledger, stub_event: StubEvent, reasoner_context: ReasonerContext
):
    version = _put(ledger, OTHER).json()["registration_version"]
    context = reasoner_context.model_copy(
        update={"reasoner_id": "other-reasoner", "label_value": 0.55}
    )
    submission = build_submission(
        stub_event,
        context,
        gate_contexts=[GateContext(gate_id="fake")],
        registration=OTHER,
    )

    receipt = ledger.submit(submission, key=OTHER_KEY)

    assert receipt.status_code == 201, receipt.text
    decision = receipt.json()
    assert decision["mode"] == "gate"
    assert decision["registration_version"] == version
    assert [g["gate_id"] for g in decision["gates"]] == ["fake"]

    _put(ledger, OTHER.model_copy(update={"novel_entity_feature": None}))
    replay = ledger.post(
        f"/api/v1/decisions/{decision['decision_id']}/replay", "", key=OTHER_KEY
    ).json()
    assert replay["matched"] is True
    assert replay["registration_version"] == version


def test_a_reasoner_key_cannot_register(ledger: Ledger):
    body = _problem(_put(ledger, OTHER, key=STUB_KEY), 403, "OPERATOR_REQUIRED")
    assert "registering a reasoner" in body["detail"]
    assert _unregistered(ledger, "other-reasoner")


def _unregistered(ledger: Ledger, reasoner_id: str) -> bool:
    response = ledger.get(f"/api/v1/reasoners/{reasoner_id}")
    return response.status_code == 404


def test_a_plugins_reasoner_cannot_be_replaced_over_http(ledger: Ledger):
    changed = STUB_REGISTRATION.model_copy(update={"reasoner_name": "Changed"})

    body = _problem(_put(ledger, changed), 409, "REGISTRATION_SOURCE_IMMUTABLE")

    assert body["source"] == "plugin:tests._stub_reasoner.plugin"
    served = ledger.get("/api/v1/reasoners/stub-reasoner").json()
    assert served["reasoner_name"] == "Stub Reasoner"


def test_a_registration_files_reasoner_cannot_be_replaced_over_http(
    two_source_ledger: Ledger,
):
    response = two_source_ledger.put(
        "/api/v1/reasoners/file-reasoner",
        json.dumps(
            {
                "reasoner_id": "file-reasoner",
                "reasoner_name": "Changed",
                "gates": [{"gate_id": "fake-lookup", "mandatory": True}],
            }
        ),
    )
    body = _problem(response, 409, "REGISTRATION_SOURCE_IMMUTABLE")
    assert body["source"].startswith("file:")


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("not json", "not a valid registration"),
        ('{"reasoner_id": "other-reasoner"}', "not a valid registration"),
        (
            '{"reasoner_id": "other-reasoner", "reasoner_name": "x", "gates": [],'
            ' "recordable_actions": ["MAYBE"]}',
            "not a valid registration",
        ),
        (
            '{"reasoner_id": "someone-else", "reasoner_name": "x", "gates": []}',
            "the path names 'other-reasoner'",
        ),
        (
            '{"reasoner_id": "other-reasoner", "reasoner_name": "x\\u0000",'
            ' "gates": []}',
            "NUL",
        ),
        (
            '{"reasoner_id": "other-reasoner", "reasoner_name": "x",'
            ' "gates": [{"gate_id": "fake", "config": {"k": NaN}}]}',
            "not a valid registration",
        ),
        (
            '{"reasoner_id": "other-reasoner", "reasoner_name": "x",'
            ' "gates": [{"gate_id": "fake", "config": {"k": [2e16]}}]}',
            "written with an exponent",
        ),
        (
            '{"reasoner_id": "other-reasoner", "reasoner_name": "x",'
            ' "gates": [{"gate_id": "fake", "config": {"k": -0.0}}]}',
            "negative zero",
        ),
    ],
    ids=[
        "not-json",
        "incomplete",
        "bad-action",
        "other-id",
        "nul",
        "nan-config",
        "exponent-float",
        "negative-zero",
    ],
)
def test_a_body_that_is_not_a_registration_for_the_path_is_refused(
    ledger: Ledger, body: str, fragment: str
):
    response = ledger.put("/api/v1/reasoners/other-reasoner", body)

    problem = _problem(response, 422, "REGISTRATION_INVALID")
    assert fragment in problem["detail"]
    assert _unregistered(ledger, "other-reasoner")


def test_a_native_namespace_declared_differently_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    widgets = NativeNamespace(
        native_namespace="widget-db",
        namespace_uuid=uuid.UUID("3f6e1b7c-9d2a-5e48-8b1f-0c7a4d9e2b61"),
        entity_type="widget",
    )
    declaring = STUB_REGISTRATION.model_copy(update={"native_namespaces": (widgets,)})

    class _Plugin:
        registration = declaring
        prompts_dir = PLUGIN.prompts_dir
        corpus_dir = PLUGIN.corpus_dir

    fake = FakeGatePlugin()
    gen = start_ledger(monkeypatch, memory_framework([_Plugin()], [fake]), fake)
    ledger = next(gen)
    try:
        conflicting = OTHER.model_copy(
            update={
                "native_namespaces": (
                    widgets.model_copy(update={"entity_type": "gadget"}),
                )
            }
        )
        body = _problem(_put(ledger, conflicting), 422, "NATIVE_NAMESPACE_CONFLICT")
        assert "'widget-db'" in body["detail"]
        assert "stub-reasoner" in body["detail"]
        assert _unregistered(ledger, "other-reasoner")
        # The same declaration is one namespace, shared: admitted.
        shared = OTHER.model_copy(update={"native_namespaces": (widgets,)})
        assert _put(ledger, shared).status_code == 201
    finally:
        gen.close()


def test_an_api_reasoner_may_change_its_own_namespace_declaration(ledger: Ledger):
    """A replaced registration is not checked against its own old version."""
    first = NativeNamespace(
        native_namespace="other-db",
        namespace_uuid=uuid.uuid4(),
        entity_type="widget",
    )
    _put(ledger, OTHER.model_copy(update={"native_namespaces": (first,)}))
    moved = first.model_copy(update={"namespace_uuid": uuid.uuid4()})

    response = _put(ledger, OTHER.model_copy(update={"native_namespaces": (moved,)}))

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("gates", "fragment"),
    [
        (
            [{"gate_id": "absent-gate", "mandatory": True}],
            "binds this gate as mandatory, but the deployment does not compose it",
        ),
        (
            [{"gate_id": "fake", "config": {"refuse": True}}],
            "scripted to refuse the binding of reasoner 'other-reasoner'",
        ),
        (
            [{"gate_id": "fake", "config": {"no_such_key": 1}}],
            "the binding's config is invalid",
        ),
    ],
    ids=["mandatory-not-composed", "gate-refuses", "config-invalid"],
)
def test_a_gate_set_the_composed_gates_cannot_serve_is_refused(
    ledger: Ledger, gates: list[dict], fragment: str
):
    body = _body(OTHER)
    data = json.loads(body)
    data["gates"] = gates
    data["bypass_rules"] = []

    response = ledger.put("/api/v1/reasoners/other-reasoner", json.dumps(data))

    problem = _problem(response, 422, "GATE_BINDING_REFUSED")
    assert problem["gate_id"] == gates[0]["gate_id"]
    assert fragment in problem["detail"]
    assert _unregistered(ledger, "other-reasoner")


def test_a_gate_set_refused_without_a_gate_leaves_every_gate_unasked(ledger: Ledger):
    """The checks that ask no gate run first: no gate learns a refused write."""
    refused = OTHER.model_copy(
        update={
            "gates": (
                GateBinding(gate_id="fake"),
                GateBinding(gate_id="absent-gate", mandatory=True),
            )
        }
    )

    problem = _problem(_put(ledger, refused), 422, "GATE_BINDING_REFUSED")

    assert problem["gate_id"] == "absent-gate"
    [gate] = ledger.gate_plugin.built
    assert all(reasoner_id != "other-reasoner" for reasoner_id, _ in gate.bindings)


def test_a_registration_body_over_the_limit_is_refused(ledger: Ledger):
    from app.api.registrations import MAX_REGISTRATION_BYTES

    body = '{"reasoner_name": "' + "x" * MAX_REGISTRATION_BYTES + '"}'

    _problem(
        ledger.put("/api/v1/reasoners/other-reasoner", body), 413, "BODY_TOO_LARGE"
    )
    assert _unregistered(ledger, "other-reasoner")


def test_an_advisory_binding_to_a_gate_not_composed_is_admitted(ledger: Ledger):
    """As at startup: that reasoner's gated decisions fail closed there."""
    advisory = OTHER.model_copy(update={"gates": (GateBinding(gate_id="absent"),)})
    assert _put(ledger, advisory).status_code == 201


def test_a_binding_configuration_reaches_the_gate_typed(ledger: Ledger):
    configured = OTHER.model_copy(
        update={"gates": (GateBinding(gate_id="fake", config={"label": "tier-2"}),)}
    )

    response = _put(ledger, configured)

    assert response.status_code == 201, response.text
    assert response.json()["registration"]["gates"] == [
        {"gate_id": "fake", "mandatory": False, "config": {"label": "tier-2"}}
    ]
    [gate] = ledger.gate_plugin.built
    assert gate.bindings[-1] == ("other-reasoner", FakeBindingConfig(label="tier-2"))


def test_a_gate_that_takes_no_binding_configuration_refuses_one(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = FakeGatePlugin()
    framework = memory_framework([PLUGIN], [fake, FakeLookupGatePlugin()])
    gen = start_ledger(monkeypatch, framework, fake)
    ledger = next(gen)
    try:
        configured = OTHER.model_copy(
            update={
                "gates": (GateBinding(gate_id="fake-lookup", config={"list": "x"}),),
                "bypass_rules": (),
            }
        )
        problem = _problem(_put(ledger, configured), 422, "GATE_BINDING_REFUSED")
        assert problem["gate_id"] == "fake-lookup"
        assert "takes no binding configuration" in problem["detail"]
    finally:
        gen.close()


def test_a_mandated_gate_that_refuses_the_empty_configuration_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    """The deployment's mandate covers reasoners registered over HTTP too."""
    fake = FakeGatePlugin()
    refusing = FakeGatePlugin(
        "fake-rules", refused_reasoners=frozenset({"other-reasoner"})
    )
    framework = memory_framework([PLUGIN], [fake, refusing], mandatory=["fake-rules"])
    gen = start_ledger(monkeypatch, framework, fake)
    ledger = next(gen)
    try:
        problem = _problem(_put(ledger, OTHER), 422, "GATE_BINDING_REFUSED")
        assert problem["gate_id"] == "fake-rules"
        assert "DECISIONLEDGER_MANDATORY_GATES" in problem["detail"]
    finally:
        gen.close()


def test_api_registrations_are_served_again_after_a_restart(
    monkeypatch: pytest.MonkeyPatch,
):
    stored = MemoryApiRegistrations()
    fake = FakeGatePlugin()
    gen = start_ledger(
        monkeypatch, memory_framework([PLUGIN], [fake], api_registrations=stored), fake
    )
    first = _put(next(gen), OTHER).json()
    gen.close()

    fake = FakeGatePlugin()
    restarted = start_ledger(
        monkeypatch, memory_framework([PLUGIN], [fake], api_registrations=stored), fake
    )
    ledger = next(restarted)
    try:
        assert ledger.get("/api/v1/reasoners/other-reasoner").json() == first
        [gate] = fake.built
        assert ("other-reasoner", FakeBindingConfig()) in gate.bindings
    finally:
        restarted.close()
