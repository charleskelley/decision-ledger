"""The ATO Reasoner's own service — a FastAPI app that submits to the ledger.

Since DR-31 the ledger mounts no reasoner router: ATO runs as its own
service. ``create_app`` builds ATO's services (Redis feature windows, the
XGBoost scorer) and a ledger client, and serves ``POST /api/v1/ato/decisions``
(``ato_reasoner.api``) plus ``GET /health``:

- with no ``ATO_LEDGER_URL`` (the default) the ledger runs in this process:
  ``LocalClient.from_settings()`` composes the deployment exactly as the
  ledger service would (installed reasoner plugins, the registration file,
  installed gates) and acts as the trusted in-process caller;
- with ``ATO_LEDGER_URL`` set, ATO calls that ledger over HTTP
  (``HttpClient``) with ``ATO_LEDGER_KEY``, its per-reasoner API key.

A refusal from the ledger (``LedgerError``) is passed through as the
ledger's own problem document, with its status and stable code; a ledger
that cannot be reached is a 503 ``LEDGER_UNAVAILABLE`` problem that names
the error's type, never its text (an ``httpx`` message can quote the key).

Run locally::

    uv run uvicorn --factory ato_reasoner.app:create_app --port 8001
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.client import HttpClient, LedgerClient, LocalClient
from app.monitoring import configure_logging
from app.settings import FrameworkSettings
from ato_reasoner.api import AtoRuntime, router
from ato_reasoner.plugin import build_ato_services
from ato_reasoner.settings import AtoSettings
from core.exceptions import LedgerError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from ato_reasoner.plugin import AtoServices

log = structlog.get_logger(__name__)


def ledger_client(settings: AtoSettings) -> LedgerClient:
    """The ledger ATO submits to, as its settings configure it.

    Args:
        settings: ATO settings (``ATO_LEDGER_URL``, ``ATO_LEDGER_KEY``).

    Returns:
        An ``HttpClient`` for ``ATO_LEDGER_URL`` when it is set; otherwise a
        ``LocalClient`` that composes the ledger in this process.
    """
    if settings.ledger_url.strip():
        return HttpClient(
            settings.ledger_url.strip(),
            api_key=settings.ledger_key.get_secret_value() or None,
        )
    return LocalClient.from_settings(FrameworkSettings())


def create_app(
    settings: AtoSettings | None = None,
    *,
    services_factory: Callable[[AtoSettings], AtoServices] = build_ato_services,
    client_factory: Callable[[AtoSettings], LedgerClient] = ledger_client,
) -> FastAPI:
    """Build the ATO service.

    Args:
        settings: ATO settings; read from the environment at startup when
            omitted.
        services_factory: Builds ATO's services (tests pass a double).
        client_factory: Builds the ledger client (tests pass one).

    Returns:
        The app; its lifespan opens the services and the client onto
        ``app.state.ato`` and closes both on shutdown.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        ato = settings if settings is not None else AtoSettings()
        framework = FrameworkSettings()
        configure_logging(json_logs=framework.log_json, level=framework.log_level)
        services = services_factory(ato)
        try:
            client = client_factory(ato)
        except Exception:
            services.close()
            raise
        application.state.ato = AtoRuntime(services=services, client=client)
        log.info(
            "ato_app.startup",
            component="ato_app",
            ledger=ato.ledger_url.strip() or "in-process",
            msg="ATO service ready",
        )
        try:
            yield
        finally:
            await client.aclose()
            services.close()
            log.info("ato_app.shutdown", component="ato_app")

    application = FastAPI(
        title="ATO Reasoner",
        description="Account-takeover risk decisions, recorded or gated by the ledger.",
        version="0.2.0",
        lifespan=lifespan,
    )

    @application.exception_handler(LedgerError)
    async def _ledger_refused(request: Request, err: LedgerError) -> JSONResponse:
        return JSONResponse(
            status_code=err.status,
            content=err.problem(instance=request.url.path),
            media_type="application/problem+json",
        )

    @application.exception_handler(httpx.HTTPError)
    async def _ledger_unreachable(
        request: Request, err: httpx.HTTPError
    ) -> JSONResponse:
        log.error(
            "ato_app.ledger_unavailable",
            component="ato_app",
            error_type=type(err).__name__,
        )
        problem = LedgerError(
            503,
            "LEDGER_UNAVAILABLE",
            f"the ledger could not be reached ({type(err).__name__})",
        )
        return JSONResponse(
            status_code=503,
            content=problem.problem(instance=request.url.path),
            media_type="application/problem+json",
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        """Liveness check."""
        return {"status": "ok"}

    application.include_router(router)
    return application
