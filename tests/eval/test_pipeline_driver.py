"""Unit tests for eval/clients/pipeline.py PipelineDriver.

Coverage scope:
- Fail-fast configuration validation in ``__init__`` (no infrastructure
  required — assertion fires before any connection).
- Stubbed RobustnessDriver methods raise ``NotImplementedError`` with
  guidance pointing at Step 4 dataset curation.

Live-pipeline behavior (run, run_event) requires Docker + OPENAI_API_KEY
and is exercised by the Step 5 scenario smoke test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.settings import Settings
from eval.clients.pipeline import PipelineDriver
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)


def _make_event() -> LoginEvent:
    return LoginEvent(
        event_id="evt-driver-test",
        timestamp=datetime.now(UTC),
        account_id="acct-driver",
        session_id="sess-driver",
        ip_address="203.0.113.20",
        geolocation=Geolocation(
            latitude=37.77,
            longitude=-122.42,
            country="US",
            city="San Francisco",
            asn="AS7922",
        ),
        device_fingerprint="fp-driver",
        user_agent="Mozilla/5.0",
        auth_method=AuthMethod.PASSWORD,
        outcome=AuthOutcome.SUCCESS,
    )


# ---------------------------------------------------------------------------
# Constructor fail-fast — no infrastructure required
# ---------------------------------------------------------------------------


def test_constructor_raises_when_openai_api_key_missing():
    """An empty OPENAI_API_KEY surfaces as ValueError before any connection."""
    settings = Settings(openai_api_key="")
    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        PipelineDriver(settings=settings)


def test_constructor_raises_when_scorer_model_missing(tmp_path):
    """A missing scorer model file surfaces as FileNotFoundError."""
    bad_path = tmp_path / "definitely-not-here.ubj"
    settings = Settings(
        openai_api_key="sk-test",
        scorer_model_path=str(bad_path),
    )
    with pytest.raises(FileNotFoundError, match="Scorer model not found"):
        PipelineDriver(settings=settings)


# ---------------------------------------------------------------------------
# RobustnessDriver stub methods — bypass __init__ to avoid infra construction
# ---------------------------------------------------------------------------


def _uninstantiated_driver() -> PipelineDriver:
    """Build a PipelineDriver shell without running __init__.

    The stub methods don't touch instance state, so they're testable
    without infrastructure construction.
    """
    return PipelineDriver.__new__(PipelineDriver)


def test_run_with_forced_schema_failure_raises_not_implemented():
    """Schema-failure stub points at the Step 4 dataset curation work."""
    driver = _uninstantiated_driver()
    with pytest.raises(NotImplementedError, match="Step 4"):
        asyncio.run(driver.run_with_forced_schema_failure(_make_event()))


def test_run_with_forced_fallback_raises_not_implemented():
    """Fallback stub points at the Step 4 dataset curation work."""
    driver = _uninstantiated_driver()
    with pytest.raises(NotImplementedError, match="Step 4"):
        asyncio.run(
            driver.run_with_forced_fallback(_make_event(), fallback_kind="llm_error")
        )


def test_fallback_stub_includes_kind_in_message():
    """Fallback stub names the requested fallback_kind for debuggability."""
    driver = _uninstantiated_driver()
    with pytest.raises(NotImplementedError, match="retrieval_timeout"):
        asyncio.run(
            driver.run_with_forced_fallback(
                _make_event(), fallback_kind="retrieval_timeout"
            )
        )
