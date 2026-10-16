"""Behavioral tests for the identity contract (``core/identity.py``, DR-32).

The framework requires every decision to carry a resolved subject identity
and implements no identity resolution itself. These tests pin the pure
contract exhaustively:

- ``derive_entity_id`` is UUID5 of the enterprise key under its namespace,
  deterministic, and refuses an empty key; a ``NativeNamespace`` derives its
  subjects' ids with it.
- ``IdentityProvenance``: a native subject is named whole or not at all; a
  confidence exactly when the method is probabilistic, in [0, 1]; a reason
  exactly when the subject is unresolved; timestamps are aware.
- ``ResolvedIdentity``: the id follows from the stated derivation (recomputed;
  a mismatch is refused); a resolved subject states its derivation; an
  unresolved one may be the reasoner's asserted id and relates to nothing;
  relationships are unique and never to the subject itself; it survives the
  JSON round trip.
- The journal: relate and unrelate are typed, merge and detach are not, and
  an event links two different ids; a membership interval ends after it
  begins.
- ``project_membership`` folds the journal and asserted relationships into
  intervals: opening is idempotent, closing what is not open is a no-op,
  merges are unordered pairs, zero-length intervals are dropped, a closed
  link can reopen, the fold is time-ordered whatever the input order, and at
  one instant the journal has the last word.
- ``linked_entities`` walks the current projection: the subject and every id
  merged with it (transitively, either direction), plus — on request — the
  subjects one relationship of a type away and their merged ids; closed
  intervals and other relationship types are ignored.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from core.identity import (
    EntityDerivation,
    EntityEvent,
    IdentityProvenance,
    JournalProvenance,
    Membership,
    NativeNamespace,
    NativeSubject,
    RelatedEntity,
    ResolvedIdentity,
    derive_entity_id,
    linked_entities,
    project_membership,
)

NS = uuid.UUID("11111111-2222-5333-8444-555555555555")
OTHER_NS = uuid.UUID("66666666-7777-5888-9999-aaaaaaaaaaaa")
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _id(name: str) -> uuid.UUID:
    return derive_entity_id(NS, name)


A, B, C, D, E, F = (_id(n) for n in "abcdef")
JOURNAL = JournalProvenance(recorded_by="test")


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _provenance(**overrides: object) -> IdentityProvenance:
    fields: dict[str, object] = {
        "native_namespace": "crm",
        "native_subject_id": "c-1",
        "resolved_by": "resolver",
        "method": "deterministic",
        "resolved_at": T0,
    }
    fields.update(overrides)
    return IdentityProvenance(**fields)  # type: ignore[arg-type]


def _identity(
    key: str = "cust-1",
    *,
    related: tuple[RelatedEntity, ...] = (),
    at: datetime = T0,
    **provenance: object,
) -> ResolvedIdentity:
    derivation = EntityDerivation(namespace_uuid=NS, enterprise_key=key)
    return ResolvedIdentity(
        entity_id=derivation.entity_id,
        entity_type="customer",
        derivation=derivation,
        provenance=_provenance(resolved_at=at, **provenance),
        related=related,
    )


def _event(
    kind: str,
    subject: uuid.UUID,
    other: uuid.UUID,
    at: datetime,
    relationship: str | None = None,
) -> EntityEvent:
    return EntityEvent(
        event_type=kind,  # type: ignore[arg-type]
        subject_entity_id=subject,
        object_entity_id=other,
        relationship_type=relationship,
        effective_at=at,
        provenance=JOURNAL,
    )


def _rel(subject: uuid.UUID, other: uuid.UUID, at: datetime, kind: str = "group"):
    return _event("relate", subject, other, at, kind)


def _unrel(subject: uuid.UUID, other: uuid.UUID, at: datetime, kind: str = "group"):
    return _event("unrelate", subject, other, at, kind)


def _interval(
    subject: uuid.UUID,
    other: uuid.UUID,
    start: datetime,
    end: datetime | None = None,
    kind: str | None = "group",
) -> Membership:
    return Membership(
        subject_entity_id=subject,
        related_entity_id=other,
        relationship_type=kind,
        valid_from=start,
        valid_to=end,
    )


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_derive_entity_id_is_uuid5_of_the_key_under_its_namespace():
    assert derive_entity_id(NS, "cust-1") == uuid.uuid5(NS, "cust-1")
    assert derive_entity_id(NS, "cust-1") == derive_entity_id(NS, "cust-1")


def test_one_key_under_two_namespaces_is_two_subjects():
    assert derive_entity_id(NS, "k") != derive_entity_id(OTHER_NS, "k")
    assert derive_entity_id(NS, "k") != derive_entity_id(NS, "k2")


def test_an_empty_enterprise_key_derives_nothing():
    with pytest.raises(ValueError, match="never empty"):
        derive_entity_id(NS, "")


def test_a_native_namespace_derives_its_subjects_ids():
    namespace = NativeNamespace(
        native_namespace="crm", namespace_uuid=NS, entity_type="customer"
    )
    assert namespace.entity_id_of("c-1") == uuid.uuid5(NS, "c-1")


@pytest.mark.parametrize(
    "fields",
    [
        {"native_namespace": "", "namespace_uuid": NS, "entity_type": "t"},
        {"native_namespace": "n", "namespace_uuid": NS, "entity_type": ""},
        {"native_namespace": "n", "namespace_uuid": str(NS), "entity_type": "t"},
        {"native_namespace": "n", "namespace_uuid": NS, "entity_type": "t", "x": 1},
    ],
)
def test_a_native_namespace_declaration_is_strict(fields):
    with pytest.raises(ValidationError):
        NativeNamespace(**fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"native_namespace": "", "native_subject_id": "k"},
        {"native_namespace": "n", "native_subject_id": ""},
        {"native_namespace": "n", "native_subject_id": 7},
        {"native_namespace": "n", "native_subject_id": "k", "x": 1},
    ],
)
def test_a_native_subject_is_a_non_empty_namespace_and_key(fields):
    with pytest.raises(ValidationError):
        NativeSubject(**fields)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_a_deterministic_match_has_no_confidence_and_no_reason():
    provenance = _provenance()
    assert provenance.confidence is None
    assert provenance.reason is None
    with pytest.raises(ValidationError, match="confidence"):
        _provenance(confidence=0.9)
    with pytest.raises(ValidationError, match="reason"):
        _provenance(reason="why")


def test_a_probabilistic_match_states_its_confidence_in_the_unit_interval():
    assert _provenance(method="probabilistic", confidence=0.87).confidence == 0.87
    with pytest.raises(ValidationError, match="confidence"):
        _provenance(method="probabilistic")
    for bad in (-0.01, 1.01, float("nan")):
        with pytest.raises(ValidationError):
            _provenance(method="probabilistic", confidence=bad)


def test_an_unresolved_subject_always_says_why():
    unresolved = _provenance(method="unresolved", reason="no match")
    assert unresolved.reason == "no match"
    with pytest.raises(ValidationError, match="reason"):
        _provenance(method="unresolved")
    with pytest.raises(ValidationError):
        _provenance(method="unresolved", reason="")


def test_a_native_subject_is_named_whole_or_not_at_all():
    named = _provenance(native_namespace=None, native_subject_id=None)
    assert named.native_namespace is None
    with pytest.raises(ValidationError, match="together"):
        _provenance(native_subject_id=None)
    with pytest.raises(ValidationError, match="together"):
        _provenance(native_namespace=None)


def test_provenance_needs_an_aware_time_and_a_known_method():
    with pytest.raises(ValidationError):
        _provenance(resolved_at=datetime(2026, 10, 1, 12, 0))
    with pytest.raises(ValidationError):
        _provenance(method="guessed")
    with pytest.raises(ValidationError):
        _provenance(resolved_by="")


# ---------------------------------------------------------------------------
# ResolvedIdentity
# ---------------------------------------------------------------------------


def test_a_resolved_identity_recomputes_its_id_from_its_derivation():
    identity = _identity("cust-1")
    assert identity.entity_id == uuid.uuid5(NS, "cust-1")
    assert identity.resolved
    with pytest.raises(ValidationError, match="not derived"):
        ResolvedIdentity(
            entity_id=uuid.uuid5(NS, "someone-else"),
            entity_type="customer",
            derivation=EntityDerivation(namespace_uuid=NS, enterprise_key="cust-1"),
            provenance=_provenance(),
        )


def test_a_resolved_subject_states_its_derivation():
    with pytest.raises(ValidationError, match="derivation"):
        ResolvedIdentity(entity_id=A, entity_type="customer", provenance=_provenance())


def test_an_unresolved_subject_may_be_the_reasoners_asserted_id():
    identity = ResolvedIdentity(
        entity_id=A,
        entity_type="account",
        provenance=_provenance(method="unresolved", reason="no match"),
    )
    assert identity.derivation is None
    assert not identity.resolved


def test_an_unresolved_subject_is_related_to_nothing():
    with pytest.raises(ValidationError, match="related to nothing"):
        ResolvedIdentity(
            entity_id=A,
            entity_type="account",
            provenance=_provenance(method="unresolved", reason="no match"),
            related=(
                RelatedEntity(relationship_type="g", entity_type="g", entity_id=B),
            ),
        )


def test_relationships_are_unique_and_never_to_the_subject_itself():
    group = RelatedEntity(relationship_type="group", entity_type="group", entity_id=B)
    other_kind = RelatedEntity(
        relationship_type="book", entity_type="book", entity_id=B
    )
    assert len(_identity(related=(group, other_kind)).related) == 2
    with pytest.raises(ValidationError, match="twice"):
        _identity(related=(group, group))
    subject = _identity().entity_id
    with pytest.raises(ValidationError, match="itself"):
        _identity(
            related=(
                RelatedEntity(
                    relationship_type="group", entity_type="g", entity_id=subject
                ),
            )
        )


def test_a_resolved_identity_survives_the_json_round_trip():
    identity = _identity(
        related=(
            RelatedEntity(relationship_type="group", entity_type="group", entity_id=B),
        ),
        method="probabilistic",
        confidence=0.75,
        crosswalk_version="cw-2",
    )
    reloaded = ResolvedIdentity.model_validate_json(identity.model_dump_json())
    assert reloaded == identity


def test_a_tampered_identity_does_not_reload():
    """The derivation is checked on reload, so a stored mapping cannot drift."""
    document = json.loads(_identity("cust-1").model_dump_json())
    document["entity_id"] = str(uuid.uuid5(NS, "cust-2"))
    with pytest.raises(ValidationError, match="not derived"):
        ResolvedIdentity.model_validate_json(json.dumps(document))


# ---------------------------------------------------------------------------
# Journal events and intervals
# ---------------------------------------------------------------------------


def test_relationships_are_typed_and_merges_are_not():
    assert _rel(A, B, T0).relationship_type == "group"
    assert _event("merge", A, B, T0).relationship_type is None
    with pytest.raises(ValidationError, match="relationship_type"):
        _event("relate", A, B, T0)
    with pytest.raises(ValidationError, match="relationship_type"):
        _event("unrelate", A, B, T0)
    with pytest.raises(ValidationError, match="relationship_type"):
        _event("merge", A, B, T0, "group")
    with pytest.raises(ValidationError, match="relationship_type"):
        _event("detach", A, B, T0, "group")


def test_an_entity_event_links_two_different_ids():
    with pytest.raises(ValidationError, match="two different"):
        _event("merge", A, A, T0)
    with pytest.raises(ValidationError):
        _event("rename", A, B, T0)


def test_an_entity_event_gets_an_id_and_needs_an_aware_time():
    first, second = _rel(A, B, T0), _rel(A, B, T0)
    assert first.event_id != second.event_id
    with pytest.raises(ValidationError):
        _rel(A, B, datetime(2026, 10, 1))


def test_a_membership_interval_ends_after_it_begins():
    assert _interval(A, B, _at(0), _at(1)).valid_to == _at(1)
    assert _interval(A, B, _at(0)).current
    with pytest.raises(ValidationError, match="ends after"):
        _interval(A, B, _at(1), _at(1))
    with pytest.raises(ValidationError, match="ends after"):
        _interval(A, B, _at(1), _at(0))
    with pytest.raises(ValidationError, match="two different"):
        _interval(A, A, _at(0))


# ---------------------------------------------------------------------------
# project_membership
# ---------------------------------------------------------------------------


def test_a_relate_opens_an_interval_and_an_unrelate_closes_it():
    assert project_membership([_rel(A, B, _at(0))]) == (_interval(A, B, _at(0)),)
    assert project_membership([_rel(A, B, _at(0)), _unrel(A, B, _at(5))]) == (
        _interval(A, B, _at(0), _at(5)),
    )


def test_relating_what_is_related_and_unrelating_what_is_not_change_nothing():
    projection = project_membership(
        [
            _unrel(A, B, _at(0)),
            _rel(A, B, _at(1)),
            _rel(A, B, _at(2)),
            _unrel(A, B, _at(3)),
            _unrel(A, B, _at(4)),
        ]
    )
    assert projection == (_interval(A, B, _at(1), _at(3)),)


def test_a_closed_link_can_open_again():
    projection = project_membership(
        [_rel(A, B, _at(0)), _unrel(A, B, _at(1)), _rel(A, B, _at(2))]
    )
    assert projection == (
        _interval(A, B, _at(0), _at(1)),
        _interval(A, B, _at(2)),
    )


def test_relationships_are_directed_and_typed():
    projection = project_membership(
        [
            _rel(A, B, _at(0)),
            _rel(B, A, _at(0)),
            _rel(A, B, _at(0), "book"),
            _unrel(B, A, _at(1)),
        ]
    )
    assert set(projection) == {
        _interval(A, B, _at(0)),
        _interval(A, B, _at(0), kind="book"),
        _interval(B, A, _at(0), _at(1)),
    }


def test_a_merge_is_an_unordered_pair_kept_in_its_opening_orientation():
    projection = project_membership(
        [
            _event("merge", A, B, _at(0)),
            _event("merge", B, A, _at(1)),  # already merged
            _event("detach", B, A, _at(2)),  # either order ends it
        ]
    )
    assert projection == (_interval(A, B, _at(0), _at(2), kind=None),)


def test_an_interval_that_would_end_as_it_begins_is_dropped():
    assert project_membership([_rel(A, B, _at(0)), _unrel(A, B, _at(0))]) == ()


def test_the_fold_is_time_ordered_whatever_the_append_order():
    projection = project_membership([_unrel(A, B, _at(5)), _rel(A, B, _at(0))])
    assert projection == (_interval(A, B, _at(0), _at(5)),)


def test_asserted_relationships_open_intervals_at_their_resolution_time():
    identity = _identity(
        "cust-1",
        at=_at(3),
        related=(
            RelatedEntity(relationship_type="group", entity_type="group", entity_id=B),
        ),
    )
    assert project_membership([], [identity]) == (
        _interval(identity.entity_id, B, _at(3)),
    )


def test_at_one_instant_the_journal_has_the_last_word():
    identity = _identity(
        "cust-1",
        at=_at(3),
        related=(
            RelatedEntity(relationship_type="group", entity_type="group", entity_id=B),
        ),
    )
    subject = identity.entity_id
    # Opened by the journal at 0, re-asserted and ended at 3: the end wins.
    assert project_membership(
        [_rel(subject, B, _at(0)), _unrel(subject, B, _at(3))], [identity]
    ) == (_interval(subject, B, _at(0), _at(3)),)
    # A later assertion reopens what the journal ended.
    later = _identity(
        "cust-1",
        at=_at(4),
        related=(
            RelatedEntity(relationship_type="group", entity_type="group", entity_id=B),
        ),
    )
    assert project_membership(
        [_rel(subject, B, _at(0)), _unrel(subject, B, _at(3))], [later]
    ) == (
        _interval(subject, B, _at(0), _at(3)),
        _interval(subject, B, _at(4)),
    )


def test_the_projection_is_sorted_by_link_then_start():
    events = [_rel(C, A, _at(2)), _rel(A, C, _at(1)), _rel(A, B, _at(0))]
    projection = project_membership(events)
    assert projection == tuple(
        sorted(
            projection,
            key=lambda m: (
                str(m.subject_entity_id),
                str(m.related_entity_id),
                m.relationship_type or "",
                m.valid_from,
            ),
        )
    )
    assert project_membership(list(reversed(events))) == projection


# ---------------------------------------------------------------------------
# linked_entities
# ---------------------------------------------------------------------------


def _edges(memberships: list[Membership]):
    def touching(ids: frozenset[uuid.UUID]) -> list[Membership]:
        return [
            m
            for m in memberships
            if m.subject_entity_id in ids or m.related_entity_id in ids
        ]

    return touching


def test_a_subject_with_no_membership_is_its_own_timeline():
    assert linked_entities(A, _edges([])) == {A: None}
    assert linked_entities(A, _edges([]), include_related="group") == {A: None}


def test_merged_ids_join_transitively_and_in_either_direction():
    memberships = [
        _interval(A, B, _at(0), kind=None),
        _interval(C, B, _at(0), kind=None),
        _interval(C, D, _at(0), kind=None),
    ]
    expected = {A: None, B: None, C: None, D: None}
    for start in (A, B, C, D):
        assert linked_entities(start, _edges(memberships)) == expected


def test_a_detached_merge_no_longer_joins():
    memberships = [_interval(A, B, _at(0), _at(1), kind=None)]
    assert linked_entities(A, _edges(memberships)) == {A: None}


def test_relationships_join_only_when_asked_for_and_only_of_that_type():
    memberships = [
        _interval(A, B, _at(0), kind="group"),
        _interval(A, C, _at(0), kind="book"),
    ]
    assert linked_entities(A, _edges(memberships)) == {A: None}
    assert linked_entities(A, _edges(memberships), include_related="group") == {
        A: None,
        B: "group",
    }
    assert linked_entities(A, _edges(memberships), include_related="other") == {A: None}


def test_a_group_sees_its_members_and_a_member_sees_its_group():
    memberships = [
        _interval(A, D, _at(0), kind="group"),
        _interval(B, D, _at(0), kind="group"),
    ]
    assert linked_entities(D, _edges(memberships), include_related="group") == {
        D: None,
        A: "group",
        B: "group",
    }
    # One step only: a member sees its group, not the group's other members.
    assert linked_entities(A, _edges(memberships), include_related="group") == {
        A: None,
        D: "group",
    }


def test_related_subjects_bring_their_merged_ids_and_merges_bring_relations():
    memberships = [
        _interval(A, B, _at(0), kind=None),  # A and B are one subject
        _interval(B, D, _at(0), kind="group"),  # ... related to group D
        _interval(D, E, _at(0), kind=None),  # D and E are one group
        _interval(F, E, _at(0), kind="group"),  # F is another member of E
    ]
    assert linked_entities(A, _edges(memberships), include_related="group") == {
        A: None,
        B: None,
        D: "group",
        E: "group",
    }
    assert linked_entities(E, _edges(memberships), include_related="group") == {
        D: None,
        E: None,
        A: "group",
        B: "group",
        F: "group",
    }


def test_closed_relationships_are_ignored():
    memberships = [_interval(A, B, _at(0), _at(1), kind="group")]
    assert linked_entities(A, _edges(memberships), include_related="group") == {A: None}


def test_the_walk_ignores_edges_the_lookup_returns_that_do_not_touch_it():
    """``edges`` may over-return; only intervals touching the walk count."""
    memberships = [
        _interval(A, B, _at(0), kind=None),
        _interval(C, D, _at(0), kind=None),
        _interval(E, F, _at(0), kind="group"),
    ]
    assert linked_entities(A, lambda ids: memberships, include_related="group") == {
        A: None,
        B: None,
    }
