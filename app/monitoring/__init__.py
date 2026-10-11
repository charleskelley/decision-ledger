"""Structured JSON logging and observability for the DecisionLedger runtime.

All log entries include: timestamp, level, component, event_id, decision_id.
Latency measurements use the duration_ms field name (standard across the project).
Uses structlog for structured output compatible with log aggregation systems.

Usage::

    from app.monitoring import configure_logging, duration_ms
    import structlog
    import time

    configure_logging()  # call once at process startup

    log = structlog.get_logger(__name__)

    t0 = time.perf_counter()
    # ... work ...
    log.info("retrieval.done", component="retrieval", duration_ms=duration_ms(t0), k=5)
"""

from __future__ import annotations

import logging
import time

import structlog


def configure_logging(*, json_logs: bool = True, level: str = "INFO") -> None:
    """Configure structlog for the DecisionLedger runtime.

    Call once at process startup before any loggers are used. Idempotent —
    safe to call multiple times (subsequent calls reconfigure structlog).

    Loggers are real ``logging.Logger`` instances (``structlog.stdlib``
    factory), so every entry carries the caller's module as ``logger`` and
    the rendered line is emitted through the stdlib root handler. That keeps
    framework and third-party output on one stream and lets tests capture
    entries with ``caplog``.

    Args:
        json_logs: When ``True`` (default), render each log entry as a single
            JSON object — suitable for log aggregation (Datadog, CloudWatch,
            Loki). When ``False``, use structlog's colored console renderer
            for local development.
        level: Minimum log level as a string (e.g. ``"INFO"``, ``"DEBUG"``).
            Applied to both the stdlib root logger and structlog's level
            filter.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_logs:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # The rendered line is already JSON (or console-formatted), so the stdlib
    # handler emits it verbatim. ``basicConfig`` is a no-op when the root
    # logger already has handlers (pytest, uvicorn), so set the level directly
    # as well to keep stdlib and structlog filtering consistent.
    logging.basicConfig(format="%(message)s", level=log_level)
    logging.getLogger().setLevel(log_level)


def duration_ms(start: float) -> float:
    """Return elapsed milliseconds since ``start``.

    Args:
        start: A ``time.perf_counter()`` value captured at the beginning of
            the operation being measured.

    Returns:
        Elapsed time in milliseconds, rounded to one decimal place.

    Example::

        import time

        t0 = time.perf_counter()
        result = do_work()
        log.info("work.done", duration_ms=duration_ms(t0))
    """
    return round((time.perf_counter() - start) * 1_000, 1)
