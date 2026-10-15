"""Registrations resource — what the deployment knows about each reasoner (DR-31).

Read-only: registrations come from the installed reasoner plugins and the
deployment's registration file at startup (``PUT`` is deferred, plan
section 9). Every registration is served with its ``registration_version``
(the content address every bundle records) and its source, and every
version a decision may cite stays readable after the registration changes.

- ``GET /api/v1/reasoners`` — every registration the deployment serves.
- ``GET /api/v1/reasoners/{reasoner_id}`` — one, as served now.
- ``GET /api/v1/reasoners/{reasoner_id}/registrations/{version}`` — one
  stored version (the registration a bundle's ``registration_version``
  names).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from app.api.errors import problem_responses
from app.api.models import (
    ReasonerList,
    RegisteredReasonerView,
    RegistrationSnapshotView,
)
from app.auth import Caller  # noqa: TC001 (FastAPI resolves the dependency)
from core.exceptions import LedgerError, UnregisteredReasonerError
from core.observation import canonical_registration

if TYPE_CHECKING:
    from app.auth import Principal
    from app.plugin import FrameworkServices
    from app.reasoner_registry import RegisteredReasoner

router = APIRouter(prefix="/api/v1", tags=["registrations"])


def _view(entry: RegisteredReasoner) -> RegisteredReasonerView:
    return RegisteredReasonerView(
        reasoner_id=entry.reasoner_id,
        reasoner_name=entry.registration.reasoner_name,
        registration_version=entry.version,
        source=entry.source,
        registration=canonical_registration(entry.registration),
    )


def list_reasoners(
    *, principal: Principal, framework: FrameworkServices
) -> ReasonerList:
    """Every registration the deployment serves, sorted by ``reasoner_id``."""
    del principal  # any authenticated caller may read the registrations
    return ReasonerList(reasoners=[_view(e) for e in framework.registry.entries()])


def get_reasoner(
    reasoner_id: str, *, principal: Principal, framework: FrameworkServices
) -> RegisteredReasonerView:
    """One registration, as served now.

    Raises:
        LedgerError: 404 ``REASONER_NOT_FOUND``.
    """
    del principal
    try:
        return _view(framework.registry.entry(reasoner_id))
    except UnregisteredReasonerError as err:
        raise LedgerError(
            404,
            "REASONER_NOT_FOUND",
            f"reasoner {reasoner_id!r} is not registered with this ledger",
        ) from err


def get_registration_version(
    reasoner_id: str,
    version: str,
    *,
    principal: Principal,
    framework: FrameworkServices,
) -> RegistrationSnapshotView:
    """One stored registration version.

    Raises:
        LedgerError: 404 ``REGISTRATION_VERSION_NOT_FOUND``.
    """
    del principal
    snapshot = framework.store.snapshots.get(reasoner_id, version)
    if snapshot is None:
        raise LedgerError(
            404,
            "REGISTRATION_VERSION_NOT_FOUND",
            f"no registration version {version!r} is stored for reasoner "
            f"{reasoner_id!r}",
        )
    return RegistrationSnapshotView(
        reasoner_id=reasoner_id,
        registration_version=snapshot.version,
        first_registered_at=snapshot.first_registered_at,
        registration=canonical_registration(snapshot.registration),
    )


@router.get("/reasoners", response_model=ReasonerList, responses=problem_responses(401))
def get_reasoners(request: Request, principal: Caller) -> ReasonerList:
    """Every reasoner registration the deployment serves."""
    return list_reasoners(principal=principal, framework=request.app.state.framework)


@router.get(
    "/reasoners/{reasoner_id}",
    response_model=RegisteredReasonerView,
    responses=problem_responses(401, 404),
)
def get_reasoner_registration(
    reasoner_id: str, request: Request, principal: Caller
) -> RegisteredReasonerView:
    """One reasoner's registration, as served now."""
    return get_reasoner(
        reasoner_id, principal=principal, framework=request.app.state.framework
    )


@router.get(
    "/reasoners/{reasoner_id}/registrations/{version}",
    response_model=RegistrationSnapshotView,
    responses=problem_responses(401, 404),
)
def get_reasoner_registration_version(
    reasoner_id: str, version: str, request: Request, principal: Caller
) -> RegistrationSnapshotView:
    """One stored version of a reasoner's registration."""
    return get_registration_version(
        reasoner_id,
        version,
        principal=principal,
        framework=request.app.state.framework,
    )
