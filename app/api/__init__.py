"""The ledger's HTTP surface (DR-31, DR-32, DR-33): decisions, subjects, gates.

Every route here is the framework's own — the ledger no longer mounts a
reasoner's router. Each route authenticates its caller (``app.auth``),
delegates to an operation function that ``app.client.LocalClient`` calls
too, and answers errors as RFC 9457 problem documents with stable codes
(``app.api.errors``).

Modules:
    decisions:     ``POST /api/v1/decisions``, ``POST /api/v1/decisions:batch``,
                   lookup, replay, and resolutions.
    entities:      ``GET /api/v1/entities/{entity_id}/decisions`` (merged ids,
                   and ``include_related``).
    identity:      ``GET /api/v1/identity/resolve`` (a native subject, through
                   the active identity resolver).
    registrations: ``GET /api/v1/reasoners``, one registration (any stored
                   version), and ``PUT /api/v1/reasoners/{reasoner_id}``
                   (operator).
    gates:         ``GET /api/v1/gates``.
    models:        The wire models the routes return and the clients parse.
    errors:        Problem documents and the handlers that produce them.
"""

from fastapi import APIRouter

from app.api import decisions, entities, gates, identity, registrations

#: Every ``/api/v1`` route of the ledger.
api_router = APIRouter()
api_router.include_router(decisions.router)
api_router.include_router(entities.router)
api_router.include_router(identity.router)
api_router.include_router(registrations.router)
api_router.include_router(gates.router)

__all__ = ["api_router"]
