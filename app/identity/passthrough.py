"""The passthrough resolver — the default, and every resolver's fallback (DR-32).

When no identity resolver is active, or the active one does not match a
subject (no match, an error, a timeout, an unusable answer), the ledger
records the subject the reasoner itself named, **unresolved**, and says why:

- a native subject in a namespace the reasoner's registration declares
  (``NativeNamespace``) is the subject that declaration derives — its native
  key under the declared UUID5 namespace, of the declared type — with the
  derivation recorded, so anyone can recompute the id from the bundle;
- any other subject is the reasoner's own ``entity_id`` and ``entity_type``,
  as its submission asserts them.

Either way the decision is written. An unresolved subject is related to
nothing; it joins an enterprise subject only through a later journal event
(a ``merge``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.exceptions import RegistrationError
from core.identity import (
    EntityDerivation,
    IdentityProvenance,
    NativeNamespace,
    ResolvedIdentity,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from core.identity import NativeSubject
    from core.observation import ReasonerRegistration
    from core.submission import Submission

#: The ``resolved_by`` of a subject the ledger kept as the reasoner named it.
PASSTHROUGH = "passthrough"

#: Why a subject is unresolved when no resolver is active.
NO_RESOLVER = "no identity resolver is active (passthrough)"

#: Why a subject is unresolved when the submission names no native subject.
NO_NATIVE_SUBJECT = (
    "the submission names no native subject, so no resolver can match it: the "
    "subject is the reasoner's entity_id"
)


def declared_identity(
    namespace: NativeNamespace,
    native: NativeSubject,
    *,
    reason: str,
    resolved_at: datetime,
) -> ResolvedIdentity:
    """The unresolved subject a declared namespace derives for a native subject.

    Args:
        namespace: The declaration of ``native.native_namespace``.
        native: The native subject.
        reason: Why the subject is unresolved.
        resolved_at: When the ledger established it.

    Returns:
        ``derive_entity_id(namespace.namespace_uuid, native_subject_id)`` of
        the declared type, with its derivation, unresolved.
    """
    derivation = EntityDerivation(
        namespace_uuid=namespace.namespace_uuid,
        enterprise_key=native.native_subject_id,
    )
    return ResolvedIdentity(
        entity_id=derivation.entity_id,
        entity_type=namespace.entity_type,
        derivation=derivation,
        provenance=IdentityProvenance(
            native_namespace=native.native_namespace,
            native_subject_id=native.native_subject_id,
            resolved_by=PASSTHROUGH,
            method="unresolved",
            resolved_at=resolved_at,
            reason=reason,
        ),
    )


def passthrough_identity(
    submission: Submission,
    registration: ReasonerRegistration,
    *,
    reason: str,
    resolved_at: datetime,
) -> ResolvedIdentity:
    """The subject the reasoner named, unresolved. Pure.

    Args:
        submission: The validated submission (intake has already checked a
            native subject in a declared namespace against its
            ``entity_id``).
        registration: The submitting reasoner's registration, whose native
            namespace declarations the derivation reads.
        reason: Why the subject is unresolved (no resolver is active, or
            what the resolver did).
        resolved_at: When the ledger established it.

    Returns:
        The declared derivation of the native subject, when its namespace is
        declared; otherwise the submission's ``entity_id`` and
        ``entity_type``.
    """
    native = submission.native_subject
    if native is not None:
        namespace = registration.native_namespace(native.native_namespace)
        if namespace is not None:
            return declared_identity(
                namespace, native, reason=reason, resolved_at=resolved_at
            )
        reason = (
            f"{reason}; the reasoner's registration does not declare namespace "
            f"{native.native_namespace!r}, so the subject is its entity_id"
        )
    return ResolvedIdentity(
        entity_id=submission.entity_id,
        entity_type=submission.entity_type,
        provenance=IdentityProvenance(
            native_namespace=native.native_namespace if native else None,
            native_subject_id=native.native_subject_id if native else None,
            resolved_by=PASSTHROUGH,
            method="unresolved",
            resolved_at=resolved_at,
            reason=reason,
        ),
    )


def declared_namespaces(
    registrations: Iterable[ReasonerRegistration],
) -> dict[str, NativeNamespace]:
    """Every native namespace the deployment's registrations declare.

    A namespace name means one thing in a deployment: registrations that
    declare the same name must declare the same UUID5 namespace and subject
    type (two reasoners that name subjects in one enterprise namespace then
    derive the same ids).

    Args:
        registrations: The registrations the deployment serves.

    Returns:
        Name → declaration.

    Raises:
        RegistrationError: Two registrations declare one namespace
            differently.
    """
    declared: dict[str, NativeNamespace] = {}
    owners: dict[str, str] = {}
    for registration in registrations:
        for namespace in registration.native_namespaces:
            name = namespace.native_namespace
            known = declared.get(name)
            if known is None:
                declared[name] = namespace
                owners[name] = registration.reasoner_id
            elif known != namespace:
                raise RegistrationError(
                    f"reasoner:{registration.reasoner_id}",
                    f"native namespace {name!r} is declared differently by "
                    f"reasoner {owners[name]!r}; one namespace name has one "
                    "UUID5 namespace and one subject type in a deployment",
                )
    return declared


__all__ = [
    "NO_NATIVE_SUBJECT",
    "NO_RESOLVER",
    "PASSTHROUGH",
    "declared_identity",
    "declared_namespaces",
    "passthrough_identity",
]
