"""Stub identity resolver plugin — widgets to owners, owners to fleets.

``PLUGIN`` satisfies ``app.identity.IdentityResolverPlugin``
(``resolver_id="stub-resolver"``, ``relationship_types={"fleet"}``); each
``build`` returns a fresh ``StubResolver`` and remembers it on
``plugin.built``. The resolver answers a native subject in the
``widget-registry`` namespace from its crosswalk (``widget key → owner
key``), deterministically, with the owner's fleet as its one related subject;
any other namespace, or a widget the crosswalk does not hold, is no match
(``None``). Its behavior is scriptable per resolver: ``delay_s`` (sleep
before answering), ``error`` (raise it), and ``answer`` (return this instead,
to hand the ledger something it must refuse).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from core.identity import (
    EntityDerivation,
    IdentityProvenance,
    RelatedEntity,
    ResolvedIdentity,
    derive_entity_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.plugin import FrameworkServices

#: The native namespace the stub resolves (the widget registry's keys).
WIDGET_REGISTRY = "widget-registry"
#: The enterprise namespaces owner and fleet ids derive under.
OWNER_NAMESPACE = UUID("fc8e0cd7-dbd8-4155-a211-129e7b1af534")
FLEET_NAMESPACE = UUID("e0fcfcfc-36c8-4d81-b0fe-d2c2de3070cd")
#: The one relationship type the stub emits.
FLEET = "fleet"
#: The crosswalk version every answer cites.
CROSSWALK_VERSION = "stub-crosswalk-1"

#: Widget key → owner key: two widgets of one owner, and a second owner.
DEFAULT_CROSSWALK = {
    "widget-12345": "owner-7",
    "widget-67890": "owner-7",
    "widget-55555": "owner-9",
}
#: Owner key → fleet key: both owners are in one fleet.
DEFAULT_FLEETS = {"owner-7": "fleet-north", "owner-9": "fleet-north"}


def owner_id(owner_key: str) -> UUID:
    """The enterprise id of an owner."""
    return derive_entity_id(OWNER_NAMESPACE, owner_key)


def fleet_id(fleet_key: str) -> UUID:
    """The enterprise id of a fleet."""
    return derive_entity_id(FLEET_NAMESPACE, fleet_key)


class StubResolverError(RuntimeError):
    """Raised by a stub resolver scripted to fail."""


class StubResolver:
    """A crosswalk lookup: widget → owner (deterministic), owner → fleet.

    Attributes:
        resolver_id: The id it stamps on every answer.
        crosswalk: Widget key → owner key.
        fleets: Owner key → fleet key.
        calls: Every ``(native_namespace, native_subject_id)`` asked, in
            order.
        delay_s: Sleep this long before answering.
        error: Raise this instead of answering.
        answer: Return this instead of the crosswalk's answer.
        closed: ``True`` once ``close()`` ran.
    """

    def __init__(
        self,
        *,
        resolver_id: str,
        crosswalk: Mapping[str, str],
        fleets: Mapping[str, str],
    ) -> None:
        self.resolver_id = resolver_id
        self.crosswalk = dict(crosswalk)
        self.fleets = dict(fleets)
        self.calls: list[tuple[str, str]] = []
        self.delay_s = 0.0
        self.error: Exception | None = None
        self.answer: object | None = None
        self.closed = False

    def resolve(
        self, native_namespace: str, native_subject_id: str
    ) -> ResolvedIdentity | None:
        """Answer from the crosswalk (or as scripted)."""
        self.calls.append((native_namespace, native_subject_id))
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        if self.answer is not None:
            return self.answer  # type: ignore[return-value]
        if native_namespace != WIDGET_REGISTRY:
            return None
        owner = self.crosswalk.get(native_subject_id)
        if owner is None:
            return None
        fleet = self.fleets.get(owner)
        return ResolvedIdentity(
            entity_id=owner_id(owner),
            entity_type="owner",
            derivation=EntityDerivation(
                namespace_uuid=OWNER_NAMESPACE, enterprise_key=owner
            ),
            provenance=IdentityProvenance(
                native_namespace=native_namespace,
                native_subject_id=native_subject_id,
                resolved_by=self.resolver_id,
                crosswalk_version=CROSSWALK_VERSION,
                method="deterministic",
                resolved_at=datetime.now(UTC),
            ),
            related=(
                (
                    RelatedEntity(
                        relationship_type=FLEET,
                        entity_type="fleet",
                        entity_id=fleet_id(fleet),
                    ),
                )
                if fleet is not None
                else ()
            ),
        )

    def close(self) -> None:
        """Mark the resolver closed (there is nothing to release)."""
        self.closed = True


class StubResolverPlugin:
    """``IdentityResolverPlugin`` for the stub resolver.

    Args:
        resolver_id: The id it registers under.
        relationship_types: The types it declares (``{"fleet"}``).
        crosswalk: Widget key → owner key.
        fleets: Owner key → fleet key.

    Attributes:
        built: Every resolver this plugin has built, in order.
    """

    def __init__(
        self,
        resolver_id: str = "stub-resolver",
        *,
        relationship_types: frozenset[str] = frozenset({FLEET}),
        crosswalk: Mapping[str, str] | None = None,
        fleets: Mapping[str, str] | None = None,
    ) -> None:
        self.resolver_id = resolver_id
        self.relationship_types = relationship_types
        self.crosswalk = dict(DEFAULT_CROSSWALK if crosswalk is None else crosswalk)
        self.fleets = dict(DEFAULT_FLEETS if fleets is None else fleets)
        self.built: list[StubResolver] = []

    def build(self, framework: FrameworkServices) -> StubResolver:
        """Build a fresh resolver over this plugin's crosswalk."""
        del framework
        resolver = StubResolver(
            resolver_id=self.resolver_id, crosswalk=self.crosswalk, fleets=self.fleets
        )
        self.built.append(resolver)
        return resolver


#: The entry-point target:
#: ``stub-resolver = "tests._stub_resolver.plugin:PLUGIN"``.
PLUGIN = StubResolverPlugin()
