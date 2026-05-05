"""Unit tests for eval/clients/pipeline.py PipelineDriver.

Coverage scope:
- Fail-fast configuration validation in ``__init__`` (no infrastructure
  required — assertion fires before any connection).
- Pure validation paths in ``run_with_forced_fallback`` (unknown
  ``fallback_kind`` rejected before any service call).

Live-pipeline behavior (``run``, ``run_event``, the live branches of the
forced-failure methods) requires Docker + OPENAI_API_KEY and is exercised
by the Step 6 scenario smoke test plus the robustness eval dimension.
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
# Pure validation in run_with_forced_fallback — bypass __init__ (no infra)
# ---------------------------------------------------------------------------


def _uninstantiated_driver() -> PipelineDriver:
    """Build a PipelineDriver shell without running __init__.

    The unknown-fallback_kind path raises before any service is touched,
    so the dispatch validation is testable without infrastructure.
    """
    return PipelineDriver.__new__(PipelineDriver)


def test_run_with_forced_fallback_rejects_unknown_kind():
    """Unknown fallback_kind raises ValueError before any service is called."""
    driver = _uninstantiated_driver()
    with pytest.raises(ValueError, match="Unknown fallback_kind"):
        asyncio.run(
            driver.run_with_forced_fallback(
                _make_event(), fallback_kind="not_a_real_kind"
            )
        )


def test_unknown_fallback_kind_message_lists_valid_kinds():
    """Error message names the supported kinds for debuggability."""
    driver = _uninstantiated_driver()
    with pytest.raises(ValueError, match="Unknown fallback_kind") as exc_info:
        asyncio.run(
            driver.run_with_forced_fallback(
                _make_event(), fallback_kind="retrieval_timeout"
            )
        )
    msg = str(exc_info.value)
    assert "retrieval_timeout" in msg
    assert "llm_5xx" in msg
    assert "retrieval_error" in msg
    assert "corpus_mismatch" in msg
