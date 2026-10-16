"""Identity contract — who a decision is about (DR-32).

DecisionLedger requires every decision to carry a resolved subject identity,
obtains it through a resolver the enterprise supplies, records the mapping
and its version on the bundle, and journals merges and detaches. It does not
implement identity resolution: the matching logic, the crosswalk, and the
master entity index belong to the enterprise. A wrong mapping becomes
visible and replayable rather than silent.

The contract, in the order a decision meets it:

- A reasoner names the subject it scored in its own terms: a
  ``NativeSubject`` (a native namespace and the subject's key in it) on its
  ``Submission``. Each namespace it uses is declared in its registration as
  a ``NativeNamespace``: the subject type the namespace names and the UUID5
  namespace its ids are derived under.
- The ledger resolves the native subject through the deployment's one
  active ``IdentityResolver`` before any gate runs and records the result,
  a ``ResolvedIdentity``, on the bundle: the decision's subject
  (``entity_id``, ``entity_type``), how the id was derived
  (``EntityDerivation``; ``derive_entity_id`` does the arithmetic and the
  model recomputes it), its ``IdentityProvenance`` (who resolved it, under
  which crosswalk version, how, and when), and the subject's flat, typed
  relationships (``RelatedEntity``). The subject is who the decision
  affects; the native subject is what the reasoner scored.
- A subject the resolver does not match (or that it fails or times out on)
  is recorded *unresolved* under the reasoner's own namespace, never
  dropped, and joins an enterprise subject only through a later journal
  event.
- The entity journal (``EntityEvent``: ``merge``, ``detach``, ``relate``,
  ``unrelate``) records changes to the identity graph, append-only; bundles
  never change. ``project_membership`` folds the journal and the
  relationships decisions asserted into ``Membership`` intervals (the
  rebuildable projection), and ``linked_entities`` walks the projection for
  a subject's timeline: the ids merged with it and, on request, the
  subjects one relationship of a type away.

The framework has no notion of what any relationship type means: a resolver
declares the types it emits when it is composed, and the ledger validates,
journals, and projects them as opaque labels.

Everything here is pure: no I/O, and nothing imported from the rest of
``core``, so every other contract may import it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    #: An operation of the membership fold: (when, source rank, position,
    #: kind, subject, object, relationship type).
    _Operation = tuple[datetime, int, int, str, UUID, UUID, str | None]

#: How a subject's identity was established: ``deterministic`` (an exact
#: crosswalk match), ``probabilistic`` (a scored match, with its confidence),
#: or ``unresolved`` (no enterprise match; the subject is the reasoner's own).
IdentityMethod = Literal["deterministic", "probabilistic", "unresolved"]

#: A change to the identity graph: ``merge`` (two ids are one subject),
#: ``detach`` (undo a merge), ``relate`` and ``unrelate`` (a typed
#: relationship from one subject to another begins or ends).
EntityEventType = Literal["merge", "detach", "relate", "unrelate"]

_MODEL_CONFIG = ConfigDict(
    strict=True, frozen=True, extra="forbid", allow_inf_nan=False
)


def derive_entity_id(namespace_uuid: UUID, enterprise_key: str) -> UUID:
    """Derive a subject's framework id from its enterprise key. Pure.

    UUID5 of ``enterprise_key`` under ``namespace_uuid``: the same key under
    the same namespace is the same id in every process, and the id does not
    reveal the key. A resolver names the namespace and the key; this
    function does the arithmetic, and ``ResolvedIdentity`` recomputes it, so
    an identity whose id does not follow from its stated derivation is
    rejected.

    Args:
        namespace_uuid: The UUID5 namespace the enterprise keys of one kind
            of subject are derived under.
        enterprise_key: The subject's key in that namespace.

    Returns:
        The subject's ``entity_id``.

    Raises:
        ValueError: ``enterprise_key`` is empty.
    """
    if not enterprise_key:
        msg = "an enterprise key is never empty"
        raise ValueError(msg)
    return uuid.uuid5(namespace_uuid, enterprise_key)


class NativeSubject(BaseModel):
    """The subject as the reasoner knows it: a key in one of its namespaces.

    Carried on a ``Submission`` (``native_subject``). The ledger resolves it
    to the decision's subject through the active identity resolver; when the
    namespace is one the reasoner's registration declares, the reasoner's
    own ``entity_id`` must be the one the declaration derives
    (``NATIVE_SUBJECT_MISMATCH`` otherwise).

    Attributes:
        native_namespace: The namespace the key belongs to, in the
            reasoner's terms (the system it comes from, for example).
        native_subject_id: The subject's key in that namespace.
    """

    model_config = _MODEL_CONFIG

    native_namespace: str = Field(min_length=1)
    native_subject_id: str = Field(min_length=1)


class NativeNamespace(BaseModel):
    """A native namespace a reasoner names its subjects in, as it registers it.

    Declared in ``ReasonerRegistration.native_namespaces``. The passthrough
    resolver (the default, and every resolver's fallback) derives the id of
    a subject named ``(native_namespace, key)`` as
    ``derive_entity_id(namespace_uuid, key)``, of type ``entity_type``.
    Within a deployment a namespace name means one thing: every
    registration that declares it declares it identically.

    Attributes:
        native_namespace: The namespace's name, as ``NativeSubject`` carries
            it.
        namespace_uuid: The UUID5 namespace its subjects' ids are derived
            under.
        entity_type: The type of subject the namespace names.
    """

    model_config = _MODEL_CONFIG

    native_namespace: str = Field(min_length=1)
    namespace_uuid: UUID
    entity_type: str = Field(min_length=1)

    def entity_id_of(self, native_subject_id: str) -> UUID:
        """The id of the subject this namespace names ``native_subject_id``."""
        return derive_entity_id(self.namespace_uuid, native_subject_id)


class IdentityProvenance(BaseModel):
    """How a decision's subject was established.

    Attributes:
        native_namespace: The namespace of the native subject the reasoner
            named; ``None`` when its submission named none.
        native_subject_id: That native subject's key; ``None`` likewise.
        resolved_by: Who established the subject: the active resolver's id,
            or ``passthrough`` when the ledger kept the reasoner's own.
        crosswalk_version: The version of the mapping the resolver used, if
            it versions one.
        method: ``deterministic``, ``probabilistic``, or ``unresolved``.
        confidence: The match confidence, in [0, 1]: set exactly when the
            method is ``probabilistic``.
        resolved_at: When the subject was established (timezone-aware).
        reason: Why the subject is unresolved (no resolver is active, the
            resolver found no match, failed, or timed out, ...): set exactly
            when the method is ``unresolved``.
    """

    model_config = _MODEL_CONFIG

    native_namespace: str | None = Field(default=None, min_length=1)
    native_subject_id: str | None = Field(default=None, min_length=1)
    resolved_by: str = Field(min_length=1)
    crosswalk_version: str | None = Field(default=None, min_length=1)
    method: IdentityMethod
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    resolved_at: AwareDatetime
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _fields_match_method(self) -> IdentityProvenance:
        """A probabilistic match has a confidence; an unresolved one, a reason."""
        if (self.native_namespace is None) != (self.native_subject_id is None):
            msg = "a native subject is named by its namespace and its id together"
            raise ValueError(msg)
        if (self.method == "probabilistic") != (self.confidence is not None):
            msg = "confidence is set exactly when the method is probabilistic"
            raise ValueError(msg)
        if (self.method == "unresolved") != (self.reason is not None):
            msg = "reason is set exactly when the method is unresolved"
            raise ValueError(msg)
        return self


class RelatedEntity(BaseModel):
    """A subject related to the decision's subject, as the resolver reports it.

    Flat from the subject and typed: the resolver declares every
    ``relationship_type`` it emits when it is composed, and the ledger
    refuses any other. Cardinality and meaning are the resolver's.

    Attributes:
        relationship_type: The relationship, one of the resolver's declared
            types.
        entity_type: The related subject's type.
        entity_id: The related subject's id.
    """

    model_config = _MODEL_CONFIG

    relationship_type: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    entity_id: UUID


class EntityDerivation(BaseModel):
    """How a subject's id follows from an enterprise key.

    Attributes:
        namespace_uuid: The UUID5 namespace.
        enterprise_key: The key under it.
    """

    model_config = _MODEL_CONFIG

    namespace_uuid: UUID
    enterprise_key: str = Field(min_length=1)

    @property
    def entity_id(self) -> UUID:
        """``derive_entity_id(namespace_uuid, enterprise_key)``."""
        return derive_entity_id(self.namespace_uuid, self.enterprise_key)


class ResolvedIdentity(BaseModel):
    """A decision's subject: the bundle's identity layer.

    Attributes:
        entity_id: The subject's id: the bundle's subject, its timeline key.
        entity_type: The subject's type (``"customer"``, ``"account"``, ...).
        derivation: The namespace and enterprise key the id derives from;
            the model recomputes ``entity_id`` from it and refuses a
            mismatch. Required of a resolved subject; ``None`` only for an
            unresolved subject that is the reasoner's asserted ``entity_id``.
        provenance: Who established the subject, how, and when.
        related: The subject's related subjects, each ``(relationship_type,
            entity_id)`` once. An unresolved subject has none.
    """

    model_config = _MODEL_CONFIG

    entity_id: UUID
    entity_type: str = Field(min_length=1)
    derivation: EntityDerivation | None = None
    provenance: IdentityProvenance
    related: tuple[RelatedEntity, ...] = ()

    @model_validator(mode="after")
    def _derivation_and_relationships_hold(self) -> ResolvedIdentity:
        """The id follows from its derivation; relationships are well formed."""
        if self.derivation is not None:
            derived = self.derivation.entity_id
            if derived != self.entity_id:
                msg = (
                    f"entity_id {self.entity_id} is not derived from its "
                    f"derivation (derive_entity_id gives {derived})"
                )
                raise ValueError(msg)
        elif self.provenance.method != "unresolved":
            msg = "a resolved subject states the derivation of its entity_id"
            raise ValueError(msg)
        if self.related and self.provenance.method == "unresolved":
            msg = "an unresolved subject is related to nothing"
            raise ValueError(msg)
        pairs = [(r.relationship_type, r.entity_id) for r in self.related]
        if len(pairs) != len(set(pairs)):
            msg = "a related subject appears twice under one relationship type"
            raise ValueError(msg)
        if any(r.entity_id == self.entity_id for r in self.related):
            msg = "a subject is not related to itself"
            raise ValueError(msg)
        return self

    @property
    def resolved(self) -> bool:
        """Whether an enterprise resolver matched the subject."""
        return self.provenance.method != "unresolved"


@runtime_checkable
class IdentityResolver(Protocol):
    """What an enterprise's identity resolver does for the ledger.

    Synchronous and fast (an in-process crosswalk lookup, typically): the
    ledger calls it in a worker thread under a time budget and records the
    subject unresolved when it does not answer in time, raises, or returns
    an identity the ledger cannot use. The resolver plugin that builds it
    declares the relationship types it emits.
    """

    def resolve(
        self, native_namespace: str, native_subject_id: str
    ) -> ResolvedIdentity | None:
        """Resolve a native subject to the enterprise subject it belongs to.

        Args:
            native_namespace: The namespace the reasoner named the subject
                in.
            native_subject_id: The subject's key in that namespace.

        Returns:
            The subject's identity — ``deterministic`` or ``probabilistic``,
            with its derivation, its provenance (echoing the native subject,
            naming this resolver in ``resolved_by``), and its related
            subjects — or ``None`` when the resolver has no match.
        """
        ...


# ---------------------------------------------------------------------------
# The entity journal and its projection
# ---------------------------------------------------------------------------


class JournalProvenance(BaseModel):
    """Who appended an entity event, and from what.

    Attributes:
        recorded_by: The writer: a resolver's id, an operator, a migration.
        crosswalk_version: The mapping version the change comes from, if
            any.
        reference: An external reference (a ticket, a source record).
    """

    model_config = _MODEL_CONFIG

    recorded_by: str = Field(min_length=1)
    crosswalk_version: str | None = Field(default=None, min_length=1)
    reference: str | None = Field(default=None, min_length=1)


class EntityEvent(BaseModel):
    """One change to the identity graph, appended to the entity journal.

    ``merge(subject, object)`` says the two ids are one subject (``subject``
    is the one kept for display); ``detach`` ends a merge of the two ids, in
    either order. ``relate(subject, object, type)`` says ``subject`` stands
    in a relationship of ``type`` to ``object``; ``unrelate`` ends it.
    Events are never updated or deleted; a correction is a new event.

    Attributes:
        event_id: The event's id.
        event_type: ``merge``, ``detach``, ``relate``, or ``unrelate``.
        subject_entity_id: The subject the change is about.
        object_entity_id: The other subject.
        relationship_type: The relationship (``relate`` and ``unrelate``
            only).
        effective_at: When the change takes effect (timezone-aware).
        provenance: Who appended it, and from what.
    """

    model_config = _MODEL_CONFIG

    event_id: UUID = Field(default_factory=uuid.uuid4)
    event_type: EntityEventType
    subject_entity_id: UUID
    object_entity_id: UUID
    relationship_type: str | None = Field(default=None, min_length=1)
    effective_at: AwareDatetime
    provenance: JournalProvenance

    @model_validator(mode="after")
    def _well_formed(self) -> EntityEvent:
        """Relationships are typed, merges are not, and no id links to itself."""
        typed = self.event_type in ("relate", "unrelate")
        if typed != (self.relationship_type is not None):
            msg = (
                "relate and unrelate name a relationship_type; merge and "
                "detach name none"
            )
            raise ValueError(msg)
        if self.subject_entity_id == self.object_entity_id:
            msg = "an entity event links two different ids"
            raise ValueError(msg)
        return self


class Membership(BaseModel):
    """One interval of the membership projection.

    Attributes:
        subject_entity_id: The subject.
        related_entity_id: The other subject.
        relationship_type: The relationship, or ``None`` for a merge (the
            two ids are one subject).
        valid_from: When the membership began.
        valid_to: When it ended; ``None`` while it holds.
    """

    model_config = _MODEL_CONFIG

    subject_entity_id: UUID
    related_entity_id: UUID
    relationship_type: str | None = Field(default=None, min_length=1)
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _interval_is_ordered(self) -> Membership:
        """An interval ends after it begins and links two different ids."""
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            msg = "a membership ends after it begins"
            raise ValueError(msg)
        if self.subject_entity_id == self.related_entity_id:
            msg = "a membership links two different ids"
            raise ValueError(msg)
        return self

    @property
    def current(self) -> bool:
        """Whether the membership holds now."""
        return self.valid_to is None


def project_membership(
    events: Iterable[EntityEvent],
    identities: Iterable[ResolvedIdentity] = (),
) -> tuple[Membership, ...]:
    """Fold the journal and the asserted relationships into membership. Pure.

    The projection is rebuildable: this function is its definition. Two
    sources feed it, merged in time order:

    - every relationship a decision's identity asserts
      (``ResolvedIdentity.related``) is a ``relate`` at the identity's
      ``resolved_at``;
    - every journal event at its ``effective_at``.

    A ``relate`` (or ``merge``) opens an interval unless one is already
    open for the same link; an ``unrelate`` (or ``detach``) closes the open
    interval, if any. A link is ``(subject, object, relationship_type)`` for
    a relationship and the unordered pair of ids for a merge. At one instant
    asserted relationships apply before journal events (so a journal event
    has the last word), journal events in the order given; an interval that
    would end the instant it began is dropped.

    Args:
        events: The journal, in append order.
        identities: The identity layers of the decisions to include.

    Returns:
        Every interval, closed and open, sorted by subject, related id,
        relationship type, and start.
    """
    operations: list[_Operation] = []
    for position, identity in enumerate(identities):
        at = identity.provenance.resolved_at
        for related in identity.related:
            operations.append(
                (
                    at,
                    0,
                    position,
                    "relate",
                    identity.entity_id,
                    related.entity_id,
                    related.relationship_type,
                )
            )
    for position, event in enumerate(events):
        operations.append(
            (
                event.effective_at,
                1,
                position,
                event.event_type,
                event.subject_entity_id,
                event.object_entity_id,
                event.relationship_type,
            )
        )
    operations.sort(key=lambda operation: operation[:3])

    opened: dict[object, tuple[UUID, UUID, str | None, datetime]] = {}
    intervals: list[Membership] = []
    for operation in operations:
        at, kind, subject, other, relationship = operation[0], *operation[3:]
        typed = kind in ("relate", "unrelate")
        link: object = (
            (subject, other, relationship) if typed else frozenset({subject, other})
        )
        if kind in ("relate", "merge"):
            opened.setdefault(link, (subject, other, relationship, at))
            continue
        start = opened.pop(link, None)
        if start is not None and at > start[3]:
            intervals.append(
                Membership(
                    subject_entity_id=start[0],
                    related_entity_id=start[1],
                    relationship_type=start[2],
                    valid_from=start[3],
                    valid_to=at,
                )
            )
    intervals.extend(
        Membership(
            subject_entity_id=subject,
            related_entity_id=other,
            relationship_type=relationship,
            valid_from=since,
        )
        for subject, other, relationship, since in opened.values()
    )
    return tuple(
        sorted(
            intervals,
            key=lambda m: (
                str(m.subject_entity_id),
                str(m.related_entity_id),
                m.relationship_type or "",
                m.valid_from,
            ),
        )
    )


def linked_entities(
    entity_id: UUID,
    edges: Callable[[frozenset[UUID]], Iterable[Membership]],
    *,
    include_related: str | None = None,
) -> dict[UUID, str | None]:
    """Every subject whose decisions belong on ``entity_id``'s timeline, now.

    Walks the current membership projection:

    1. ``entity_id`` and every id merged with it, transitively and in
       either direction (one subject, several ids);
    2. with ``include_related``, every subject one current relationship of
       that type away from any of those ids, in either direction (a
       subject's group, or a group's members), each with its own merged
       ids.

    Args:
        entity_id: The subject.
        edges: Returns the membership intervals touching any of the given
            ids (subject or related side). It may return more, and
            intervals that no longer hold; both are ignored.
        include_related: A relationship type to follow one step, or
            ``None``.

    Returns:
        Each linked id mapped to how it is linked: ``None`` for the subject
        and the ids merged with it, ``include_related`` for a related
        subject (and its merged ids).
    """

    def _merged(seeds: set[UUID]) -> set[UUID]:
        seen = set(seeds)
        frontier = set(seeds)
        while frontier:
            reached: set[UUID] = set()
            for interval in edges(frozenset(frontier)):
                if not interval.current or interval.relationship_type is not None:
                    continue
                ends = {interval.subject_entity_id, interval.related_entity_id}
                if ends & frontier:
                    reached |= ends - seen
            seen |= reached
            frontier = reached
        return seen

    own = _merged({entity_id})
    linked: dict[UUID, str | None] = dict.fromkeys(own)
    if include_related is None:
        return linked
    neighbors: set[UUID] = set()
    for interval in edges(frozenset(own)):
        if not interval.current or interval.relationship_type != include_related:
            continue
        ends = {interval.subject_entity_id, interval.related_entity_id}
        if ends & own:
            neighbors |= ends - own
    if neighbors:
        for related in _merged(neighbors) - own:
            linked[related] = include_related
    return linked


__all__ = [
    "EntityDerivation",
    "EntityEvent",
    "EntityEventType",
    "IdentityMethod",
    "IdentityProvenance",
    "IdentityResolver",
    "JournalProvenance",
    "Membership",
    "NativeNamespace",
    "NativeSubject",
    "RelatedEntity",
    "ResolvedIdentity",
    "derive_entity_id",
    "linked_entities",
    "project_membership",
]
