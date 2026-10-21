"""Registrations resource — what the deployment knows about each reasoner.

Registrations come from three sources (DR-31, DR-33): the installed reasoner
plugins and the deployment's registration file, which are declared
configuration, and the API, which writes them at runtime. Every
registration is served with its ``registration_version`` (the content
address every bundle records) and its source, and every version a decision
may cite stays readable after the registration changes.

- ``GET /api/v1/reasoners`` — every registration the deployment serves.
- ``GET /api/v1/reasoners/{reasoner_id}`` — one, as served now.
- ``PUT /api/v1/reasoners/{reasoner_id}`` — register a reasoner, or replace
  its API registration (operator key; DR-33). The body is a
  ``ReasonerRegistration`` as JSON, the shape of a registration-file entry.
  The write is admitted exactly as a registration is at startup: no other
  registration may declare one of its native namespaces differently, a gate
  it binds as mandatory must be composed, every composed gate it binds must
  admit the binding and serve the reasoner, and every gate the deployment
  mandates must accept it. ``201`` creates, ``200`` replaces (a new
  version, with its own snapshot) or finds the registration unchanged; a
  refusal stores nothing. A plugin's or the file's registration cannot be
  replaced (``409 REGISTRATION_SOURCE_IMMUTABLE``), and nothing deletes a
  registration (bundles reference registrations).
- ``GET /api/v1/reasoners/{reasoner_id}/registrations/{version}`` — one
  stored version (the registration a bundle's ``registration_version``
  names).
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from app.api.decisions import read_body
from app.api.errors import invalid, problem_responses
from app.api.models import (
    ReasonerList,
    RegisteredReasonerView,
    RegistrationSnapshotView,
)
from app.auth import Caller, require_operator
from app.gates import check_gate_set
from app.identity import declared_namespaces
from app.reasoner_registry import API_SOURCE
from core.exceptions import (
    GatePluginError,
    LedgerError,
    RegistrationError,
    UnregisteredReasonerError,
)
from core.observation import (
    ReasonerRegistration,
    canonical_registration,
    registration_version,
)
from core.submission import storage_problem

if TYPE_CHECKING:
    from app.auth import Principal
    from app.plugin import FrameworkServices
    from app.reasoner_registry import RegisteredReasoner

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["registrations"])

#: Largest registration body, in bytes.
MAX_REGISTRATION_BYTES = 1 << 20

#: From this magnitude Python writes a float with an exponent, which
#: ``jsonb`` reads back as an integer.
_FLOAT_EXPONENT_FROM = 1e16


def _view(entry: RegisteredReasoner) -> RegisteredReasonerView:
    return RegisteredReasonerView(
        reasoner_id=entry.reasoner_id,
        reasoner_name=entry.registration.reasoner_name,
        registration_version=entry.version,
        source=entry.source,
        registered_by=entry.registered_by,
        registered_at=entry.registered_at,
        updated_at=entry.updated_at,
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


def parse_registration(reasoner_id: str, body: bytes | str) -> ReasonerRegistration:
    """Parse the registration a write carries, strictly, for ``reasoner_id``.

    Raises:
        LedgerError: 422 ``REGISTRATION_INVALID`` when the body is not a
            valid ``ReasonerRegistration`` (with the validation errors),
            registers another reasoner than the path names, or holds a value
            the ledger cannot store.
    """
    try:
        registration = ReasonerRegistration.model_validate_json(body)
    except ValidationError as err:
        raise invalid(
            "REGISTRATION_INVALID", "the body is not a valid registration", err
        ) from err
    if registration.reasoner_id != reasoner_id:
        raise LedgerError(
            422,
            "REGISTRATION_INVALID",
            f"the body registers reasoner {registration.reasoner_id!r}, but the "
            f"path names {reasoner_id!r}",
            extra={"reasoner_id": reasoner_id},
        )
    canonical = canonical_registration(registration)
    problem = storage_problem(canonical) or _rewritten_number(canonical)
    if problem is not None:
        raise LedgerError(
            422,
            "REGISTRATION_INVALID",
            f"the registration holds {problem}, which the ledger cannot store",
            extra={"reasoner_id": reasoner_id},
        )
    return registration


def _rewritten_number(value: object) -> str | None:
    """A number Postgres ``jsonb`` would store as another value, or ``None``.

    ``jsonb`` stores numbers as ``numeric``: a float written with an exponent
    (from 1e16 up) comes back as an integer, and ``-0.0`` as ``0.0``. Either
    would give the snapshot a different content address than the version
    the write reported, so a write may not carry one.
    """
    if isinstance(value, float):
        if value == 0.0 and math.copysign(1.0, value) < 0:
            return "a negative zero (-0.0)"
        if abs(value) >= _FLOAT_EXPONENT_FROM:
            return (
                f"a number written with an exponent ({value!r}); write it as an integer"
            )
        return None
    if isinstance(value, dict):
        items: list[object] = list(value.values())
    elif isinstance(value, list):
        items = list(value)
    else:
        return None
    return next(
        (problem for item in items if (problem := _rewritten_number(item))), None
    )


def admit_registration(
    registration: ReasonerRegistration, *, framework: FrameworkServices
) -> None:
    """Run the startup admission checks on one registration (DR-33).

    The checks the bootstrap runs for every registration it serves: one
    native namespace means one thing across the deployment (the other
    registrations served now, the one being replaced excluded), and the
    composed gates can serve the gate set (``check_gate_set``: a mandatory
    gate composed, every bound composed gate admitting the binding and
    serving the reasoner, every mandated gate accepting it). The caller
    holds the registry's lock.

    Raises:
        LedgerError: 422 ``NATIVE_NAMESPACE_CONFLICT`` or
            ``GATE_BINDING_REFUSED`` (with the gate's id and reason).
    """
    reasoner_id = registration.reasoner_id
    others = [
        entry.registration
        for entry in framework.registry.entries()
        if entry.reasoner_id != reasoner_id
    ]
    try:
        declared_namespaces([*others, registration])
    except RegistrationError as err:
        raise LedgerError(
            422,
            "NATIVE_NAMESPACE_CONFLICT",
            err.reason,
            extra={"reasoner_id": reasoner_id},
        ) from err
    try:
        check_gate_set(registration, framework.gates)
    except GatePluginError as err:
        raise LedgerError(
            422,
            "GATE_BINDING_REFUSED",
            f"gate {err.gate_id!r}: {err.reason}",
            extra={"reasoner_id": reasoner_id, "gate_id": err.gate_id},
        ) from err


def register_reasoner(
    reasoner_id: str,
    body: bytes | str,
    *,
    principal: Principal,
    framework: FrameworkServices,
) -> tuple[RegisteredReasonerView, bool]:
    """Register a reasoner over the API, or replace its API registration.

    Admission and the write happen under the registry's lock, so two writes
    never admit against the same state. A registration identical to the one
    served now is left as it is (no new version, nothing written). Otherwise
    the registry stores the new version's snapshot and the reasoner's row
    before it serves the version; a decision already deciding keeps the
    registration it read, and replay enforces every bundle against the
    snapshot of the version it cites.

    Args:
        reasoner_id: The reasoner the path names.
        body: A ``ReasonerRegistration`` as JSON text.
        principal: The caller (operator or trusted).
        framework: The ledger's services.

    Returns:
        The registration as served now, and ``True`` when the write created
        the reasoner's registration.

    Raises:
        LedgerError: 403 ``OPERATOR_REQUIRED``; 422 ``REGISTRATION_INVALID``,
            ``NATIVE_NAMESPACE_CONFLICT``, or ``GATE_BINDING_REFUSED``; 409
            ``REGISTRATION_SOURCE_IMMUTABLE`` (a plugin's or the registration
            file's reasoner).
    """
    require_operator(principal, act="registering a reasoner")
    registration = parse_registration(reasoner_id, body)
    registry = framework.registry
    with registry.lock:
        current = (
            registry.entry(reasoner_id) if registry.is_registered(reasoner_id) else None
        )
        if current is not None and current.source != API_SOURCE:
            raise LedgerError(
                409,
                "REGISTRATION_SOURCE_IMMUTABLE",
                f"reasoner {reasoner_id!r} is registered by {current.source}, "
                "which is the deployment's declared configuration; it cannot be "
                "replaced over the API",
                extra={"reasoner_id": reasoner_id, "source": current.source},
            )
        if current is not None and current.version == registration_version(
            registration
        ):
            return _view(current), False
        admit_registration(registration, framework=framework)
        registry.register(registration, registered_by=principal.label)
        entry = registry.entry(reasoner_id)
    log.info(
        "api.reasoner_registered",
        component="api",
        reasoner_id=reasoner_id,
        registration_version=entry.version,
        previous_version=current.version if current is not None else None,
        principal=principal.label,
    )
    return _view(entry), current is None


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


@router.put(
    "/reasoners/{reasoner_id}",
    response_model=RegisteredReasonerView,
    responses={
        201: {
            "description": "Registered: the reasoner's first registration",
            "model": RegisteredReasonerView,
        },
        **problem_responses(401, 403, 409, 413, 422),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ReasonerRegistration"}
                }
            },
        }
    },
)
async def put_reasoner_registration(
    reasoner_id: str, request: Request, response: Response, principal: Caller
) -> RegisteredReasonerView:
    """Register a reasoner, or replace its API registration (operator key).

    ``201`` with a ``Location`` when the reasoner was not registered; ``200``
    when its API registration was replaced or is unchanged.
    """
    body = await read_body(request, limit=MAX_REGISTRATION_BYTES)
    view, created = await asyncio.to_thread(
        register_reasoner,
        reasoner_id,
        body,
        principal=principal,
        framework=request.app.state.framework,
    )
    if created:
        response.status_code = 201
        response.headers["Location"] = f"/api/v1/reasoners/{reasoner_id}"
    return view


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
