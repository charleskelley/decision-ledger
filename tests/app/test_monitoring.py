"""Behavioral tests for the structured logging configuration.

``configure_logging`` is called once per process by the API lifespan. The
regression these tests guard: a processor chain that reads ``logger.name``
paired with a logger factory whose loggers have no ``name`` makes the first
log call after configuration raise ``AttributeError``.
"""

from __future__ import annotations

import json
import logging

import structlog

from app.monitoring import configure_logging, duration_ms


def test_configure_logging_then_logging_once_does_not_raise(caplog):
    configure_logging(json_logs=True, level="INFO")

    with caplog.at_level(logging.INFO):
        structlog.get_logger("tests.monitoring.probe").info(
            "probe.event", component="tests", decision_id="abc"
        )

    assert any("probe.event" in rec.getMessage() for rec in caplog.records)


def test_json_entries_carry_logger_name_level_and_bound_fields(caplog):
    configure_logging(json_logs=True, level="INFO")

    with caplog.at_level(logging.INFO):
        structlog.get_logger("tests.monitoring.fields").info(
            "fields.event", component="tests", duration_ms=1.5
        )

    record = next(r for r in caplog.records if "fields.event" in r.getMessage())
    entry = json.loads(record.getMessage())
    assert entry["event"] == "fields.event"
    assert entry["logger"] == "tests.monitoring.fields"
    assert entry["level"] == "info"
    assert entry["component"] == "tests"
    assert entry["duration_ms"] == 1.5
    assert "timestamp" in entry


def test_console_renderer_path_also_logs_without_error(caplog):
    configure_logging(json_logs=False, level="DEBUG")

    with caplog.at_level(logging.DEBUG):
        structlog.get_logger("tests.monitoring.console").debug("console.event")

    assert any("console.event" in rec.getMessage() for rec in caplog.records)


def test_configure_logging_is_idempotent(caplog):
    configure_logging()
    configure_logging(json_logs=True, level="WARNING")

    with caplog.at_level(logging.WARNING):
        structlog.get_logger("tests.monitoring.twice").warning("twice.event")

    assert any("twice.event" in rec.getMessage() for rec in caplog.records)


def test_duration_ms_is_non_negative_and_rounded_to_one_decimal():
    import time

    t0 = time.perf_counter()
    elapsed = duration_ms(t0)

    assert elapsed >= 0.0
    assert elapsed == round(elapsed, 1)
