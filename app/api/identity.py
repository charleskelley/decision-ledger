"""Identity route — resolve a native subject the way a decision would (DR-32).

``GET /api/v1/identity/resolve?namespace=<native namespace>&native_id=<key>``
answers with the subject the deployment's active identity resolver
establishes for a native subject: the ``ResolvedIdentity`` a decision naming
it would record (its id and type, how the id derives, the provenance of the
mapping, and its related subjects). When the resolver does not match it (or
no resolver is active), a namespace one of the deployment's registrations
declares yields the passthrough subject, unresolved, with the reason; any
other native subject is ``404 IDENTITY_UNRESOLVED``.

Any authenticated caller may resolve: an identity is the enterprise's
mapping, not a tenant's decisions. A subject selector (search by any native
id, then read the subject's timeline) is built on this route and
``GET /api/v1/entities/{entity_id}/decisions``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request

from app.api.errors import problem_responses
from app.auth import Caller  # noqa: TC001 (FastAPI resolves the dependency)
from app.identity import declared_namespaces, native_subject
from core.exceptions import LedgerError
from core.identity import ResolvedIdentity

if TYPE_CHECKING:
    from app.auth import Principal
    from app.plugin import FrameworkServices

router = APIRouter(prefix="/api/v1", tags=["identity"])


async def resolve_identity(
    namespace: str,
    native_id: str,
    *,
    principal: Principal,
    framework: FrameworkServices,
) -> ResolvedIdentity:
    """Resolve a native subject through the deployment's identity step.

    Args:
        namespace: The native namespace.
        native_id: The subject's key in it.
        principal: The caller (any authenticated principal may resolve).
        framework: The ledger's services.

    Returns:
        The active resolver's match, or the passthrough subject of a
        namespace a registration declares (unresolved, with the reason).

    Raises:
        LedgerError: 422 ``REQUEST_INVALID`` for an empty or unstorable
            parameter; 404 ``IDENTITY_UNRESOLVED`` when the resolver does not
            match the subject and no registration declares its namespace.
    """
    del principal  # every authenticated caller may resolve
    try:
        native = native_subject(namespace, native_id)
    except ValueError as err:
        raise LedgerError(
            422,
            "REQUEST_INVALID",
            str(err),
            extra={"errors": [{"loc": ["query"], "msg": str(err)}]},
        ) from err
    namespaces = declared_namespaces(
        entry.registration for entry in framework.registry.entries()
    )
    result = await framework.identity.lookup(native, namespaces=namespaces)
    if result.identity is None:
        raise LedgerError(
            404,
            "IDENTITY_UNRESOLVED",
            (
                f"no identity for this subject in namespace {namespace!r}: "
                f"{result.reason}, and no registration declares the namespace"
            ),
            extra={
                "native_namespace": namespace,
                "resolved_by": framework.identity.resolver_id,
            },
        )
    return result.identity


@router.get(
    "/identity/resolve",
    response_model=ResolvedIdentity,
    responses=problem_responses(401, 404, 422),
)
async def get_identity_resolve(
    request: Request,
    principal: Caller,
    namespace: str = Query(min_length=1),
    native_id: str = Query(min_length=1),
) -> ResolvedIdentity:
    """The subject a native subject resolves to, as a decision would record it."""
    return await resolve_identity(
        namespace,
        native_id,
        principal=principal,
        framework=request.app.state.framework,
    )
