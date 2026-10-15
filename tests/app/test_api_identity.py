"""The identity contract over HTTP (DR-32): resolution, provenance, timelines.

The real app over in-memory services, with the stub resolver
(``tests/_stub_resolver``: widgets in ``widget-registry`` belong to owners,
owners to fleets) and the in-memory entity journal. Pinned:

- ``GET /api/v1/identity/resolve`` answers with the active resolver's match;
  a declared namespace's passthrough subject when nothing matches; ``404
  IDENTITY_UNRESOLVED`` otherwise (without echoing the native key); ``422``
  for a missing or empty parameter; ``401`` without a key.
- A decision naming a native subject is filed under the subject the resolver
  established (the receipt's ``entity_id``, the bundle's ``identity`` with
  its provenance); one naming an unknown namespace is recorded unresolved
  under the reasoner's own id, and still written.
- ``GET /api/v1/entities/{id}/decisions`` unions the ids the journal merged
  with the subject, and with ``include_related=<type>`` the subjects one
  relationship of that type away; each decision says which subject it is
  about and how that subject is linked. A reasoner key still reads only its
  own decisions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 (dataclass field type)
from typing import TYPE_CHECKING

import pytest

from app.auth import KEY_HEADER
from core.identity import (
    EntityEvent,
    JournalProvenance,
    NativeNamespace,
    NativeSubject,
)
from core.observation import ReasonerRegistration
from tests._fake_gate.plugin import FakeGatePlugin
from tests._memory_ledger import MemoryEntityJournal, memory_framework
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION
from tests._stub_resolver.plugin import (
    FLEET,
    WIDGET_REGISTRY,
    StubResolverPlugin,
    fleet_id,
    owner_id,
)
from tests.app.conftest import OTHER_KEY, start_ledger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.submission import Submission
    from tests.app.conftest import Ledger

WIDGET_NS = NativeNamespace(
    native_namespace="widget-db",
    namespace_uuid=uuid.UUID("3f6e1b7c-9d2a-5e48-8b1f-0c7a4d9e2b61"),
    entity_type="widget",
)


@dataclass
class _DeclaringPlugin:
    """The stub reasoner, registered with a declared native namespace."""

    registration: ReasonerRegistration
    prompts_dir: Path = PLUGIN.prompts_dir
    corpus_dir: Path = PLUGIN.corpus_dir


def _declaring() -> _DeclaringPlugin:
    fields = {
        name: getattr(STUB_REGISTRATION, name)
        for name in ReasonerRegistration.model_fields
    }
    return _DeclaringPlugin(
        ReasonerRegistration(**{**fields, "native_namespaces": (WIDGET_NS,)})
    )


@pytest.fixture
def resolving(monkeypatch: pytest.MonkeyPatch) -> Iterator[Ledger]:
    """The ledger with the stub resolver active."""
    gate_plugin = FakeGatePlugin()
    framework = memory_framework(
        [PLUGIN], [gate_plugin], resolver_plugin=StubResolverPlugin()
    )
    yield from start_ledger(monkeypatch, framework, gate_plugin)


@pytest.fixture
def declaring(monkeypatch: pytest.MonkeyPatch) -> Iterator[Ledger]:
    """The ledger with no resolver and a reasoner that declares a namespace."""
    gate_plugin = FakeGatePlugin()
    framework = memory_framework([_declaring()], [gate_plugin])
    yield from start_ledger(monkeypatch, framework, gate_plugin)


def _widget(submission: Submission, key: str, n: int) -> Submission:
    return submission.model_copy(
        update={
            "event_id": f"evt-{key}-{n}",
            "native_subject": NativeSubject(
                native_namespace=WIDGET_REGISTRY, native_subject_id=key
            ),
        }
    )


def _resolve(ledger: Ledger, namespace: str, native_id: str, **kw):
    return ledger.get(
        "/api/v1/identity/resolve",
        params={"namespace": namespace, "native_id": native_id},
        **kw,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/identity/resolve
# ---------------------------------------------------------------------------


def test_resolve_answers_with_the_resolvers_match(resolving: Ledger):
    response = _resolve(resolving, WIDGET_REGISTRY, "widget-12345")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entity_id"] == str(owner_id("owner-7"))
    assert body["entity_type"] == "owner"
    assert body["provenance"]["resolved_by"] == "stub-resolver"
    assert body["provenance"]["method"] == "deterministic"
    assert body["related"] == [
        {
            "relationship_type": FLEET,
            "entity_type": "fleet",
            "entity_id": str(fleet_id("fleet-north")),
        }
    ]


def test_resolve_finds_nothing_for_an_unmatched_undeclared_subject(resolving: Ledger):
    response = _resolve(resolving, "other-registry", "secret-key-42")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "IDENTITY_UNRESOLVED"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "secret-key-42" not in response.text
    assert body["native_namespace"] == "other-registry"
    assert body["resolved_by"] == "stub-resolver"


def test_resolve_answers_a_declared_namespace_with_its_passthrough_subject(
    declaring: Ledger,
):
    response = _resolve(declaring, "widget-db", "widget-12345")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entity_id"] == str(WIDGET_NS.entity_id_of("widget-12345"))
    assert body["entity_type"] == "widget"
    assert body["provenance"]["method"] == "unresolved"
    assert body["provenance"]["resolved_by"] == "passthrough"
    assert body["derivation"] == {
        "namespace_uuid": str(WIDGET_NS.namespace_uuid),
        "enterprise_key": "widget-12345",
    }


@pytest.mark.parametrize(
    "params",
    [{"namespace": "n"}, {"native_id": "k"}, {"namespace": "", "native_id": "k"}],
    ids=["no-id", "no-namespace", "empty-namespace"],
)
def test_resolve_needs_both_parameters(resolving: Ledger, params):
    response = resolving.get("/api/v1/identity/resolve", params=params)
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_INVALID"


def test_resolve_needs_a_key(resolving: Ledger):
    response = _resolve(resolving, WIDGET_REGISTRY, "widget-12345", key=None)
    assert response.status_code == 401


def test_any_key_may_resolve(resolving: Ledger):
    response = _resolve(resolving, WIDGET_REGISTRY, "widget-12345", key=OTHER_KEY)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Decisions are filed under the resolved subject
# ---------------------------------------------------------------------------


def test_a_decision_is_filed_under_the_subject_the_resolver_established(
    resolving: Ledger, submission
):
    response = resolving.submit(_widget(submission, "widget-12345", 1))
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["entity_id"] == str(owner_id("owner-7"))

    bundle = resolving.get(f"/api/v1/decisions/{receipt['decision_id']}").json()
    identity = bundle["identity"]
    assert identity["entity_id"] == str(owner_id("owner-7"))
    assert identity["provenance"]["native_namespace"] == WIDGET_REGISTRY
    assert identity["provenance"]["native_subject_id"] == "widget-12345"
    assert identity["provenance"]["crosswalk_version"] == "stub-crosswalk-1"
    assert bundle["submission"]["entity_id"] == str(submission.entity_id)

    replay = resolving.post(f"/api/v1/decisions/{receipt['decision_id']}/replay", "")
    assert replay.status_code == 200
    assert replay.json()["matched"] is True


def test_an_unknown_namespace_is_recorded_unresolved_and_still_written(
    resolving: Ledger, submission
):
    unknown = submission.model_copy(
        update={
            "native_subject": NativeSubject(
                native_namespace="unknown-registry", native_subject_id="w-9"
            )
        }
    )
    response = resolving.submit(unknown)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["entity_id"] == str(submission.entity_id)
    identity = resolving.get(f"/api/v1/decisions/{receipt['decision_id']}").json()[
        "identity"
    ]
    assert identity["provenance"]["method"] == "unresolved"
    assert identity["provenance"]["resolved_by"] == "passthrough"
    assert "found no match" in identity["provenance"]["reason"]


def test_a_contradicted_declared_namespace_is_refused(declaring: Ledger, submission):
    contradicted = submission.model_copy(
        update={
            "native_subject": NativeSubject(
                native_namespace="widget-db", native_subject_id="widget-99999"
            )
        }
    )
    response = declaring.submit(contradicted)
    assert response.status_code == 422
    assert response.json()["code"] == "NATIVE_SUBJECT_MISMATCH"


# ---------------------------------------------------------------------------
# Timelines: merged ids and related subjects
# ---------------------------------------------------------------------------


def _timeline(ledger: Ledger, entity_id: uuid.UUID | str, **params):
    response = ledger.get(f"/api/v1/entities/{entity_id}/decisions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _decided(ledger: Ledger, submission: Submission) -> str:
    response = ledger.submit(submission)
    assert response.status_code == 201, response.text
    return response.json()["decision_id"]


def test_a_group_timeline_covers_its_members(resolving: Ledger, submission):
    seven_a = _decided(resolving, _widget(submission, "widget-12345", 1))
    seven_b = _decided(resolving, _widget(submission, "widget-67890", 2))
    nine = _decided(resolving, _widget(submission, "widget-55555", 3))
    fleet = fleet_id("fleet-north")

    own = _timeline(resolving, owner_id("owner-7"))
    assert {d["decision_id"] for d in own["decisions"]} == {seven_a, seven_b}
    assert {d["relationship_type"] for d in own["decisions"]} == {None}
    assert {d["entity_type"] for d in own["decisions"]} == {"owner"}

    assert _timeline(resolving, fleet)["decisions"] == []
    group = _timeline(resolving, fleet, include_related=FLEET)
    assert group["include_related"] == FLEET
    assert {d["decision_id"] for d in group["decisions"]} == {seven_a, seven_b, nine}
    by_id = {d["decision_id"]: d for d in group["decisions"]}
    assert by_id[nine]["entity_id"] == str(owner_id("owner-9"))
    assert {d["relationship_type"] for d in group["decisions"]} == {FLEET}

    # One relationship away only: owner-7 sees its fleet, not owner-9.
    member = _timeline(resolving, owner_id("owner-7"), include_related=FLEET)
    assert {d["decision_id"] for d in member["decisions"]} == {seven_a, seven_b}


def test_a_merge_brings_an_unresolved_subjects_decisions_onto_the_timeline(
    resolving: Ledger, submission
):
    resolved = _decided(resolving, _widget(submission, "widget-12345", 1))
    stray = submission.model_copy(
        update={
            "event_id": "evt-stray",
            "native_subject": NativeSubject(
                native_namespace="legacy-registry", native_subject_id="W-12345"
            ),
        }
    )
    unresolved = _decided(resolving, stray)
    owner = owner_id("owner-7")
    assert {d["decision_id"] for d in _timeline(resolving, owner)["decisions"]} == {
        resolved
    }

    journal = MemoryEntityJournal(
        resolving.store, relationship_types=frozenset({FLEET})
    )
    journal.append(
        [
            EntityEvent(
                event_type="merge",
                subject_entity_id=owner,
                object_entity_id=submission.entity_id,
                effective_at=datetime.now(UTC),
                provenance=JournalProvenance(recorded_by="operator"),
            )
        ]
    )

    merged = _timeline(resolving, owner)["decisions"]
    assert {d["decision_id"] for d in merged} == {resolved, unresolved}
    stray_row = next(d for d in merged if d["decision_id"] == unresolved)
    assert stray_row["entity_id"] == str(submission.entity_id)
    assert stray_row["relationship_type"] is None
    # ...and from the merged id's side too.
    from_stray = _timeline(resolving, submission.entity_id)["decisions"]
    assert {d["decision_id"] for d in from_stray} == {resolved, unresolved}


def test_the_journal_refuses_a_relationship_type_the_resolver_does_not_declare(
    resolving: Ledger,
):
    journal = MemoryEntityJournal(
        resolving.store, relationship_types=frozenset({FLEET})
    )
    with pytest.raises(ValueError, match="does not declare"):
        journal.append(
            [
                EntityEvent(
                    event_type="relate",
                    subject_entity_id=uuid.uuid4(),
                    object_entity_id=uuid.uuid4(),
                    relationship_type="segment",
                    effective_at=datetime.now(UTC),
                    provenance=JournalProvenance(recorded_by="operator"),
                )
            ]
        )
    assert journal.events() == []


def test_include_related_names_a_relationship_type(resolving: Ledger):
    response = resolving.get(
        f"/api/v1/entities/{uuid.uuid4()}/decisions", params={"include_related": ""}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_INVALID"


def test_a_reasoner_key_reads_only_its_own_decisions_on_a_group_timeline(
    resolving: Ledger, submission
):
    _decided(resolving, _widget(submission, "widget-12345", 1))
    response = resolving.client.get(
        f"/api/v1/entities/{fleet_id('fleet-north')}/decisions",
        params={"include_related": FLEET},
        headers={KEY_HEADER: OTHER_KEY},
    )
    assert response.status_code == 200
    assert response.json()["decisions"] == []
