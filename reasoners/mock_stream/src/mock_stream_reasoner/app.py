r"""The stream mock's own service — a FastAPI app that runs the stream.

``create_app`` opens the in-memory stream (``source``), the per-account
state (``state``), and a ledger client, and starts the consumer task that
decides each accepted event in arrival order (``pipeline.StreamProcessor``);
the routes are in ``mock_stream_reasoner.api``, plus ``GET /health``.

The mock always calls the ledger over HTTP (``HttpClient`` with
``MOCK_STREAM_LEDGER_URL`` and ``MOCK_STREAM_LEDGER_KEY``): it is the
reference pattern of a reasoner the ledger never imports, whichever way the
ledger registers it (its plugin, a registration file, or a PUT). A blank URL
refuses to start.

On shutdown the source closes, the consumer finishes the events already
accepted (for at most ``DRAIN_SECONDS``), and the client closes.

Run locally::

    MOCK_STREAM_LEDGER_URL=http://localhost:8000 MOCK_STREAM_LEDGER_KEY=... \
        uv run python -m mock_stream_reasoner serve --port 8002
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI

from app.client import HttpClient, LedgerClient
from app.monitoring import configure_logging
from app.settings import FrameworkSettings
from mock_stream_reasoner.api import StreamRuntime, router
from mock_stream_reasoner.pipeline import StreamProcessor
from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION
from mock_stream_reasoner.settings import MockStreamSettings
from mock_stream_reasoner.source import EventSource
from mock_stream_reasoner.state import AccountWindows

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

log = structlog.get_logger(__name__)

#: How long shutdown waits for the consumer to finish the accepted events.
DRAIN_SECONDS = 30.0


def ledger_client(settings: MockStreamSettings) -> LedgerClient:
    """The ledger the mock submits to: always over HTTP.

    Args:
        settings: The mock's settings (``MOCK_STREAM_LEDGER_URL``,
            ``MOCK_STREAM_LEDGER_KEY``).

    Returns:
        An ``HttpClient`` for the ledger, carrying the mock's key.
    """
    return HttpClient(
        settings.ledger_url,
        api_key=settings.ledger_key.get_secret_value() or None,
    )


def _log_consumer_exit(task: asyncio.Task[None]) -> None:
    """Log a consumer that stopped on an error (it never should)."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "mock_stream_app.consumer_failed",
            component="mock_stream_app",
            error_type=type(error).__name__,
        )


def create_app(
    settings: MockStreamSettings | None = None,
    *,
    client_factory: Callable[[MockStreamSettings], LedgerClient] = ledger_client,
) -> FastAPI:
    """Build the stream mock's service.

    Args:
        settings: The mock's settings; read from the environment (and
            ``.env``) at startup when omitted.
        client_factory: Builds the ledger client from the settings (tests
            pass one that returns a client over an in-process ledger).

    Returns:
        A FastAPI app whose lifespan opens the stream, starts its consumer,
        and closes both, and the client, on exit.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved = settings if settings is not None else MockStreamSettings()
        process = FrameworkSettings()
        configure_logging(json_logs=process.log_json, level=process.log_level)
        client = client_factory(resolved)
        window = timedelta(seconds=resolved.window_seconds)
        lateness = timedelta(seconds=resolved.allowed_lateness_seconds)
        source = EventSource(allowed_lateness=lateness)
        processor = StreamProcessor(
            client,
            # Kept whole for the window plus the lateness bound: an accepted
            # event never trails the latest by more, so it is scored exactly.
            windows=AccountWindows(window=window, retention=window + lateness),
            history=resolved.history,
        )
        consumer = asyncio.create_task(
            processor.run(source), name="mock-stream-consumer"
        )
        consumer.add_done_callback(_log_consumer_exit)
        application.state.stream = StreamRuntime(
            settings=resolved,
            client=client,
            source=source,
            processor=processor,
            consumer=consumer,
        )
        if not resolved.ledger_key.get_secret_value():
            log.warning(
                "mock_stream_app.no_ledger_key",
                component="mock_stream_app",
                msg="MOCK_STREAM_LEDGER_KEY is blank: the ledger will refuse "
                "every submission (401)",
            )
        log.info(
            "mock_stream_app.startup",
            component="mock_stream_app",
            ledger=resolved.ledger_url,
            msg="stream mock ready",
        )
        try:
            yield
        finally:
            source.close()
            try:
                await asyncio.wait_for(consumer, timeout=DRAIN_SECONDS)
            except TimeoutError:
                log.warning(
                    "mock_stream_app.drain_timeout",
                    component="mock_stream_app",
                    pending=source.pending,
                )
            except Exception as err:  # the consumer died; its callback logged it
                log.warning(
                    "mock_stream_app.consumer_stopped",
                    component="mock_stream_app",
                    error_type=type(err).__name__,
                )
            finally:
                await client.aclose()
            log.info("mock_stream_app.shutdown", component="mock_stream_app")

    application = FastAPI(
        title="Mock Stream Reasoner",
        description=(
            "The real-time event scorer pattern (DR-35): an in-memory stream of "
            "transfers, recorded or gated by the ledger."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "reasoner_id": MOCK_STREAM_REGISTRATION.reasoner_id}

    application.include_router(router)
    return application
