"""Fixtures for the rules gate member's tests.

The gate is reasoner-agnostic, so these tests use a minimal local
Observation (``WidgetObservation``) rather than any reasoner package or the
framework's test doubles. Rule-set files are written to a temporary
directory per test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, computed_field

from core.gate_request import GateRequest
from core.observation import (
    GateContext,
    LabelType,
    ReasonerContext,
    ReasonerRegistration,
)
from core.routes import GateRoute

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

REASONER_ID = "widget-reasoner"

#: A rule set in the documented YAML shape, registered for ``REASONER_ID``.
RULESET: dict[str, Any] = {
    "ruleset_id": "widget-guardrails",
    "version": "1.0.0",
    "reasoner_id": REASONER_ID,
    "description": "Hold thin-history widgets; block extreme scores.",
    "default_action": "ALLOW",
    "rules": [
        {
            "rule_id": "thin-history-hold",
            "description": "Too little history to trust the score.",
            "when": {
                "kind": "compare",
                "field": "feature_set.thin_history",
                "op": "eq",
                "value": True,
            },
            "action": "HOLD",
            "required_controls": ["calibration_review"],
        },
        {
            "rule_id": "extreme-score-block",
            "when": {
                "kind": "compare",
                "field": "label_value",
                "op": "gt",
                "value": 0.9,
            },
            "action": "BLOCK",
            "required_controls": ["notify_owner"],
        },
        {
            "rule_id": "wobbly-region-challenge",
            "when": {
                "kind": "all",
                "predicates": [
                    {
                        "kind": "compare",
                        "field": "feature_set.region",
                        "op": "in",
                        "value": ["REGION_A", "REGION_B"],
                    },
                    {
                        "kind": "compare",
                        "field": "feature_set.wobble",
                        "op": "ge",
                        "value": 5,
                    },
                ],
            },
            "action": "CHALLENGE",
        },
    ],
}

_NAMESPACE = UUID("6a1d1f9e-2b0c-5d7e-9f3a-4c8b7e6d5a21")


class WidgetObservation(BaseModel):
    """A minimal Observation: just the fields the framework protocol needs."""

    model_config = ConfigDict(strict=True, frozen=True)

    event_id: str
    entity_type: str = "widget"
    timestamp: datetime
    widget_id: str
    route: GateRoute = GateRoute.ROUTE_TO_GATE
    reasoner_context: ReasonerContext | None = None
    fast_path_rationale: str | None = None
    gate_context: GateContext

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """Framework UUID derived from ``widget_id``."""
        return uuid.uuid5(_NAMESPACE, self.widget_id)


def make_observation(
    *,
    label_value: float = 0.5,
    reasoner_id: str = REASONER_ID,
    ruleset_id: str = "widget-guardrails",
    **features: float | int | str | bool,
) -> WidgetObservation:
    """A widget observation routed to the rules gate."""
    feature_set: dict[str, float | int | str | bool] = {
        "thin_history": False,
        "region": "REGION_C",
        "wobble": 1,
    }
    feature_set.update(features)
    return WidgetObservation(
        event_id=f"evt-{uuid.uuid4().hex[:8]}",
        timestamp=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        widget_id="widget-1",
        reasoner_context=ReasonerContext(
            reasoner_id=reasoner_id,
            reasoner_name="Widget Reasoner",
            model_version="widget-scorer-v1",
            label_type=LabelType.NUMERICAL,
            label_name="widget_score",
            label_value=label_value,
            feature_set=feature_set,
        ),
        gate_context=GateContext(
            gate_id="rules", gate_config={"ruleset_id": ruleset_id}
        ),
    )


def make_request(observation: WidgetObservation) -> GateRequest:
    """The request the framework builds for ``observation``."""
    return GateRequest(
        decision_id="dec-rules-001",
        observation=observation,
        gate_context=observation.gate_context,
        budget_ms=1_000,
    )


@pytest.fixture
def ruleset_data() -> dict[str, Any]:
    """A deep copy of ``RULESET`` a test may modify."""
    return yaml.safe_load(yaml.safe_dump(RULESET))


@pytest.fixture
def observation_for() -> Callable[..., WidgetObservation]:
    """``make_observation`` as a fixture (test modules cannot import conftest)."""
    return make_observation


@pytest.fixture
def request_for() -> Callable[[WidgetObservation], GateRequest]:
    """``make_request`` as a fixture."""
    return make_request


@pytest.fixture
def registration() -> ReasonerRegistration:
    """A registration that allows the rules gate and declares no profile."""
    return ReasonerRegistration(
        reasoner_id=REASONER_ID,
        reasoner_name="Widget Reasoner",
        allowed_gate_ids=frozenset({"rules"}),
        allowed_prompt_template_ids=frozenset(),
        allowed_jurisdictions=frozenset(),
    )


@pytest.fixture
def write_ruleset(tmp_path: Path) -> Callable[..., Path]:
    """Write one rule-set YAML file into ``tmp_path``; return the directory."""

    def _write(
        data: dict[str, Any] | None = None, name: str = "guardrails.yaml"
    ) -> Path:
        (tmp_path / name).write_text(
            yaml.safe_dump(data if data is not None else RULESET), encoding="utf-8"
        )
        return tmp_path

    return _write
