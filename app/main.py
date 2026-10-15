"""FastAPI application factory — composes the framework with discovered plugins.

``create_app`` discovers reasoner plugins (``decisionledger.reasoners``
entry points, filtered by ``DECISIONLEDGER_REASONERS``) and gate plugins
(``decisionledger.gates`` entry points), mounts each reasoner plugin's
router, and installs a lifespan that builds the shared framework services
once — composed gates included — and then asks every reasoner plugin to
build its own services against them. Nothing in this module names a
concrete reasoner or gate.

Generic framework routes (health, decision lookup, replay) live here since
they key on ``decision_id`` only. Lookup returns a bundle of any schema
version as stored; replay serves schema version 2 and refuses older bundles
with 409 ``BUNDLE_SCHEMA_UNSUPPORTED``. Reasoner-specific decision endpoints
live in each reasoner's own router.

Run locally::

    uv run uvicorn --factory app.main:create_app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ValidationError

from app.audit import as_bundle
from app.bootstrap import build_framework_services
from app.gates import discover_gates
from app.monitoring import configure_logging
from app.plugins import allowlist_from_settings, discover_plugins
from app.settings import FrameworkSettings
from core.bundle import BUNDLE_SCHEMA_VERSION
from core.exceptions import BundleSchemaUnsupportedError, UnregisteredReasonerError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from app.audit.store import BundleStore
    from app.gates import GatePlugin
    from app.plugin import FrameworkServices, ReasonerPlugin, ReasonerServices

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Framework-side response models (generic — keyed on decision_id)
# ---------------------------------------------------------------------------


class GateReplayResponse(BaseModel):
    """One gate's original and replayed enforced action."""

    gate_id: str
    status: str
    original_action: str | None
    replayed_action: str | None


class ReplayResponse(BaseModel):
    """Result of replaying enforcement against a bundle's cached states.

    ``matched`` is the replay verdict: the decision action, every gate's
    enforced action, and (for a record) the bypass rule check all
    reproduced.
    """

    decision_id: str
    original_action: str
    replayed_action: str
    actions_match: bool
    original_bypass_rule_holds: bool | None
    bypass_rule_holds: bool | None
    gates: list[GateReplayResponse]
    matched: bool


class ErrorResponse(BaseModel):
    """Consistent error envelope."""

    error_code: str
    message: str
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Framework-side endpoints (health, decision lookup, replay)
# ---------------------------------------------------------------------------

framework_router = APIRouter()


def _store(request: Request) -> BundleStore:
    """Return the bundle store from the framework services on ``app.state``."""
    framework: FrameworkServices = request.app.state.framework
    return framework.store


def _not_found(decision_id: str, err: KeyError) -> HTTPException:
    """404 envelope: no bundle exists for ``decision_id``."""
    return HTTPException(
        status_code=404,
        detail={
            "error_code": "DECISION_NOT_FOUND",
            "message": f"No decision found for id={decision_id!r}",
        },
    )


def _not_composed(decision_id: str, err: UnregisteredReasonerError) -> HTTPException:
    """409 envelope: the bundle exists but its reasoner is not in this deployment.

    A legitimate multi-tenant state: the row was written by a reasoner that
    is installed elsewhere or excluded by ``DECISIONLEDGER_REASONERS``, so
    this process has no registration (enforcement profile) to replay it
    with. Distinct from 404 so operators can tell "no such decision" from
    "wrong deployment". Lookup needs no registration and never returns it.
    """
    return HTTPException(
        status_code=409,
        detail={
            "error_code": "REASONER_NOT_COMPOSED",
            "message": (
                f"Decision {decision_id!r} belongs to reasoner "
                f"{err.reasoner_id!r}, which is not composed in this deployment"
            ),
            "details": {"reasoner_id": err.reasoner_id},
        },
    )


def _unsupported_schema(err: BundleSchemaUnsupportedError) -> HTTPException:
    """409 envelope: the bundle predates the schema this operation needs."""
    return HTTPException(
        status_code=409,
        detail={
            "error_code": "BUNDLE_SCHEMA_UNSUPPORTED",
            "message": str(err),
            "details": {
                "schema_version": err.schema_version,
                "supported_schema_version": err.supported,
            },
        },
    )


def _unreadable(
    decision_id: str, err: ValidationError, schema_version: int
) -> HTTPException:
    """500 envelope: a stored row the current bundle model no longer reads.

    The store refuses to write a bundle that does not reload, so this is a
    row written under an older model, or edited by hand: say so, with its id.
    """
    return HTTPException(
        status_code=500,
        detail={
            "error_code": "BUNDLE_UNREADABLE",
            "message": (
                f"Decision {decision_id!r} is stored as schema version "
                f"{schema_version} but does not validate against it"
            ),
            "details": {"errors": err.error_count()},
        },
    )


@framework_router.get("/health")
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@framework_router.get(
    "/api/v1/decisions/{decision_id}",
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
def get_decision(decision_id: str, request: Request) -> dict[str, Any]:
    """Retrieve a stored DecisionBundle by ID (any reasoner, any schema version).

    A schema-version-2 bundle is validated and returned in full; an older
    one is returned exactly as stored, with its ``schema_version``. Neither
    needs the reasoner that wrote it to be composed here.

    Args:
        decision_id: UUID of the decision to retrieve.
        request: FastAPI request (for accessing app.state services).

    Returns:
        The DecisionBundle as JSON, ``schema_version`` included.

    Raises:
        HTTPException: 404 ``DECISION_NOT_FOUND`` when no bundle exists; 500
            ``BUNDLE_UNREADABLE`` when a schema-version-2 row does not
            validate.
    """
    try:
        stored = _store(request).lookup(decision_id)
    except KeyError as err:
        raise _not_found(decision_id, err) from err
    try:
        return as_bundle(stored).model_dump(mode="json")
    except BundleSchemaUnsupportedError:
        return {**stored.payload, "schema_version": stored.schema_version}
    except ValidationError as err:
        raise _unreadable(decision_id, err, stored.schema_version) from err


@framework_router.post(
    "/api/v1/decisions/{decision_id}/replay",
    response_model=ReplayResponse,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
def replay_decision(decision_id: str, request: Request) -> ReplayResponse:
    """Replay enforcement against a bundle's cached states (any reasoner).

    Re-runs the deterministic enforcement layer on every gate output the
    bundle stores, re-evaluates a record's bypass rule against its stored
    evidence, and recombines. No gate (and no LLM) is re-invoked. A mismatch
    indicates a non-determinism bug in enforcement.

    Args:
        decision_id: UUID of the decision to replay.
        request: FastAPI request (for accessing app.state services).

    Returns:
        ReplayResponse with the original vs replayed comparison.

    Raises:
        HTTPException: 404 ``DECISION_NOT_FOUND`` when no bundle exists;
            409 ``BUNDLE_SCHEMA_UNSUPPORTED`` for a bundle older than schema
            version 2; 409 ``REASONER_NOT_COMPOSED`` when the bundle's
            reasoner is not part of this deployment; 500
            ``BUNDLE_UNREADABLE`` when its schema-version-2 row does not
            validate.
    """
    try:
        result = _store(request).replay(decision_id)
    except KeyError as err:
        raise _not_found(decision_id, err) from err
    except BundleSchemaUnsupportedError as err:
        raise _unsupported_schema(err) from err
    except UnregisteredReasonerError as err:
        raise _not_composed(decision_id, err) from err
    except ValidationError as err:  # only a version-2 row reaches validation
        raise _unreadable(decision_id, err, BUNDLE_SCHEMA_VERSION) from err
    return ReplayResponse(
        decision_id=result.decision_id,
        original_action=result.original_action,
        replayed_action=result.replayed_action,
        actions_match=result.actions_match,
        original_bypass_rule_holds=result.original_bypass_rule_holds,
        bypass_rule_holds=result.bypass_rule_holds,
        gates=[
            GateReplayResponse(
                gate_id=g.gate_id,
                status=g.status,
                original_action=g.original_action,
                replayed_action=g.replayed_action,
            )
            for g in result.gates
        ],
        matched=result.matched,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    plugins: Sequence[ReasonerPlugin] | None = None,
    gate_plugins: Sequence[GatePlugin] | None = None,
) -> FastAPI:
    """Build the DecisionLedger FastAPI application.

    Args:
        plugins: Reasoner plugins to compose. Defaults to every plugin
            installed under the ``decisionledger.reasoners`` entry point
            group, filtered by ``DECISIONLEDGER_REASONERS``. Tests pass an
            explicit list.
        gate_plugins: Gate plugins to compose. Defaults to every plugin
            installed under the ``decisionledger.gates`` entry point group.
            Tests pass an explicit list (a fake gate, or none).

    Returns:
        A FastAPI app whose lifespan builds the framework services (composed
        gates included) and each reasoner plugin's services, exposing them on
        ``app.state.framework`` and ``app.state.reasoners[reasoner_id]``.

    Raises:
        ReasonerPluginError: A discovered reasoner plugin is malformed.
        GatePluginError: A discovered gate plugin is malformed.
    """
    if plugins is None:
        plugins = discover_plugins(allow=allowlist_from_settings(FrameworkSettings()))
    composed = list(plugins)
    composed_gates = (
        list(gate_plugins) if gate_plugins is not None else discover_gates()
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Construct framework + reasoner services at startup; close on exit."""
        settings = FrameworkSettings()
        configure_logging(json_logs=settings.log_json, level=settings.log_level)
        # Logged here, after configure_logging, so the warning goes through
        # the structured renderer rather than structlog's import-time default.
        if not composed:
            log.warning(
                "api.no_reasoners",
                component="api",
                msg=(
                    "No reasoner plugins discovered — only framework routes "
                    "(health, lookup, replay) are available"
                ),
            )

        framework = build_framework_services(settings, composed, composed_gates)
        reasoners: dict[str, ReasonerServices] = {}
        try:
            for plugin in composed:
                reasoner_id = plugin.registration.reasoner_id
                reasoners[reasoner_id] = plugin.build_services(framework)
        except Exception:
            _close_all(reasoners, framework)
            raise

        application.state.framework = framework
        application.state.reasoners = reasoners
        log.info(
            "api.startup",
            component="api",
            reasoner_ids=sorted(reasoners),
            gate_ids=sorted(framework.gates),
            msg="All services initialized",
        )

        yield

        _close_all(reasoners, framework)
        log.info("api.shutdown", component="api", msg="Connections closed")

    application = FastAPI(
        title="DecisionLedger",
        description="Model-agnostic governed decision API.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(framework_router)
    for plugin in composed:
        application.include_router(plugin.router)
    return application


def _close_all(
    reasoners: dict[str, ReasonerServices], framework: FrameworkServices
) -> None:
    """Close every reasoner's services, then the framework's."""
    for services in reasoners.values():
        services.close()
    framework.close()
