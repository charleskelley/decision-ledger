"""FastAPI application factory — the ledger's HTTP service (DR-31).

``create_app`` discovers reasoner plugins (``decisionledger.reasoners``
entry points, filtered by ``DECISIONLEDGER_REASONERS``) and gate plugins
(``decisionledger.gates`` entry points), and installs a lifespan that builds
the shared framework services once — the registry (plugins plus the
registration file), the composed gates, the store, and the resolution
journal — and the key ring the routes authenticate callers with. Nothing in
this module names a concrete reasoner or gate.

The ledger serves its own routes only: ``GET /health`` and the ``/api/v1``
surface (``app/api``). It mounts no reasoner router and builds no reasoner
services: a reasoner runs as its own service and calls
``POST /api/v1/decisions`` (``app.client.HttpClient``), or runs the same
operations in process (``app.client.LocalClient``). Every error is an RFC
9457 problem document with a stable ``code``.

Run locally::

    uv run uvicorn --factory app.main:create_app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi

from app.api import api_router
from app.api.errors import install_problem_handlers
from app.api.models import ProblemDocument, ResolutionRequest
from app.auth import KEY_HEADER, KeyRing
from app.bootstrap import build_framework_services
from app.gates import discover_gates
from app.monitoring import configure_logging
from app.plugins import allowlist_from_settings, discover_plugins
from app.settings import FrameworkSettings
from core.submission import Submission

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from app.gates import GatePlugin
    from app.plugin import ReasonerPlugin

log = structlog.get_logger(__name__)

health_router = APIRouter()


@health_router.get("/health")
def health() -> dict[str, str]:
    """Liveness check (unauthenticated)."""
    return {"status": "ok"}


#: Models the routes read from raw bodies (or answer with) that FastAPI does
#: not see as parameters; the OpenAPI document carries them as components.
_WIRE_MODELS = (Submission, ResolutionRequest, ProblemDocument)


def _install_openapi(application: FastAPI) -> None:
    """Generate the OpenAPI document with the wire models as components.

    The decision and resolution routes parse their bodies themselves
    (strictly, as JSON), so FastAPI does not know their schemas; their
    request bodies reference ``#/components/schemas/<Model>`` and every
    problem response references ``ProblemDocument``. This registers those
    models (and every model they nest) under ``components.schemas``.
    """

    def _openapi() -> dict[str, Any]:
        if application.openapi_schema is None:
            document = get_openapi(
                title=application.title,
                version=application.version,
                description=application.description,
                routes=application.routes,
            )
            schemas = document.setdefault("components", {}).setdefault("schemas", {})
            for model in _WIRE_MODELS:
                exported = model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
                for name, definition in exported.pop("$defs", {}).items():
                    schemas.setdefault(name, definition)
                schemas[model.__name__] = exported
            application.openapi_schema = document
        return application.openapi_schema

    application.openapi = _openapi


def create_app(
    plugins: Sequence[ReasonerPlugin] | None = None,
    gate_plugins: Sequence[GatePlugin] | None = None,
    *,
    keyring: KeyRing | None = None,
) -> FastAPI:
    """Build the DecisionLedger FastAPI application.

    Args:
        plugins: Reasoner plugins to compose (their registrations and the
            assets their gates read). Defaults to every plugin installed
            under the ``decisionledger.reasoners`` entry point group,
            filtered by ``DECISIONLEDGER_REASONERS``. Tests pass an explicit
            list.
        gate_plugins: Gate plugins to compose. Defaults to every plugin
            installed under the ``decisionledger.gates`` entry point group.
            Tests pass an explicit list (a fake gate, or none).
        keyring: The API keys to authenticate callers with. Defaults to the
            keys the settings configure (``DECISIONLEDGER_API_KEYS``,
            ``DECISIONLEDGER_API_KEYS_FILE``, ``DECISIONLEDGER_OPERATOR_KEY``),
            read at startup.

    Returns:
        A FastAPI app whose lifespan builds the framework services onto
        ``app.state.framework`` and the key ring onto ``app.state.keyring``.

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
        """Construct the framework services and the key ring; close on exit."""
        settings = FrameworkSettings()
        configure_logging(json_logs=settings.log_json, level=settings.log_level)
        ring = keyring if keyring is not None else KeyRing.from_settings(settings)
        framework = build_framework_services(settings, composed, composed_gates)
        served = [entry.reasoner_id for entry in framework.registry.entries()]
        # Logged after configure_logging, so the warnings go through the
        # structured renderer rather than structlog's import-time default.
        if not served:
            log.warning(
                "api.no_reasoners",
                component="api",
                msg="No reasoner is registered: every submission will be refused",
            )
        if ring.empty:
            log.warning(
                "api.no_keys",
                component="api",
                msg=(
                    f"No API key is configured: every /api/v1 request answers "
                    f"401 (send keys with {KEY_HEADER}; see .env.example)"
                ),
            )
        unkeyed = sorted(set(served) - ring.reasoner_ids)
        if unkeyed and not ring.empty:
            log.info(
                "api.reasoners_without_keys",
                component="api",
                reasoner_ids=unkeyed,
            )

        application.state.framework = framework
        application.state.keyring = ring
        log.info(
            "api.startup",
            component="api",
            reasoner_ids=served,
            gate_ids=sorted(framework.gates),
            msg="All services initialized",
        )

        yield

        framework.close()
        log.info("api.shutdown", component="api", msg="Connections closed")

    application = FastAPI(
        title="DecisionLedger",
        description=(
            "Decision system of record with a gating service: record a "
            "decision a reasoner made, or ask the ledger to gate one; look "
            "decisions up by id or by subject, replay them, and resolve held "
            "ones."
        ),
        version="0.3.0",
        lifespan=lifespan,
    )
    install_problem_handlers(application)
    application.include_router(health_router)
    application.include_router(api_router)
    _install_openapi(application)
    return application
