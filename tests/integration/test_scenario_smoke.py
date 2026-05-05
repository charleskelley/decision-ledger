"""End-to-end smoke test — every canonical scenario lands on its expected action.

Drives a deterministic 5-event sequence per scenario through the live
``PipelineDriver`` and asserts the trigger event's ``decision_action``
falls inside the scenario's declared ``expected_actions`` set. This is
the integration regression catcher and the single most valuable test in
the suite for the project narrative.

Prerequisites:
    docker compose up -d
    make build-policy-index
    export OPENAI_API_KEY=sk-...
    export ANTHROPIC_API_KEY=sk-ant-...

The session-scoped ``driver`` fixture amortizes service construction
(Redis/PG/ES connections, embedding model, cross-encoder load) across
all 8 parameterizations — the full smoke run finishes in well under
60 seconds with the stack warm.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from eval.clients.pipeline import PipelineDriver
from generator.factory import EventFactory
from generator.loader import load_scenario

if TYPE_CHECKING:
    from collections.abc import Iterator


# Canonical scenarios from generator/scenarios/. Each YAML declares its
# expected_actions; the smoke test exercises that contract per scenario.
SCENARIO_IDS: tuple[str, ...] = (
    "baseline_normal",
    "high_velocity_legitimate",
    "device_fingerprint_anomaly",
    "geo_impossible",
    "credential_stuffing_burst",
    "novel_entity",
    "post_breach_ato",
    "adversarial_probe",
)


@pytest.fixture(scope="session")
def driver() -> Iterator[PipelineDriver]:
    """Session-scoped PipelineDriver — heavy services constructed once."""
    d = PipelineDriver()
    try:
        yield d
    finally:
        d.close()


@pytest.mark.smoke
@pytest.mark.integration
@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
async def test_scenario_lands_on_expected_action(
    scenario_id: str,
    driver: PipelineDriver,
) -> None:
    """The scenario's trigger event must land on a declared expected_action.

    Generates 5 events deterministically (fixed seed=42 + fixed start
    timestamp) so reruns are bit-identical: the only source of variance
    is the gate's LLM call, which the scorer's fast-path may bypass for
    extreme-confidence routes.
    """
    config = load_scenario(scenario_id)
    factory = EventFactory(
        config,
        account_ids=[f"acct-smoke-{scenario_id}"],
        seed=42,
    )
    base_ts = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
    events = [
        factory.build_event(
            f"acct-smoke-{scenario_id}",
            base_ts + timedelta(minutes=i * 5),
        )
        for i in range(5)
    ]

    result = await driver.run(events)

    expected = {a.value for a in config.expected_actions}
    assert result.decision_action.value in expected, (
        f"scenario={scenario_id} produced {result.decision_action.value!r}, "
        f"expected one of {sorted(expected)}"
    )
