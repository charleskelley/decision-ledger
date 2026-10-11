"""StubEvent — the stub reasoner's Observation type.

``StubEvent`` satisfies the framework ``Observation`` protocol structurally
(PEP 544): ``event_id``, ``entity_id`` (a UUID5 derived from ``widget_id``),
``entity_type``, ``timestamp``, ``route``, ``reasoner_context``,
``fast_path_rationale``, and ``gate_context``. It is the minimal shape a
reasoner needs; a real reasoner adds whatever domain attributes its pipeline
consumes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field

from core.observation import GateContext, ReasonerContext
from core.routes import GateRoute

#: UUID5 namespace for stub widget identities. Distinct from every real
#: reasoner's namespace so stub and production entity ids can never collide.
STUB_WIDGET_NAMESPACE: UUID = UUID("3f6e1b7c-9d2a-5e48-8b1f-0c7a4d9e2b61")


class StubEvent(BaseModel):
    """A widget observation submitted by the stub reasoner.

    Attributes:
        event_id: Unique identifier for this event instance.
        entity_type: Framework subject classification. Always ``"widget"``.
        timestamp: When the widget was observed (UTC).
        widget_id: Stub domain business key; ``entity_id`` is derived from it.
        route: Gate route chosen by the stub pipeline.
        reasoner_context: Generic evidence record. ``None`` before assembly;
            the framework raises if it is ``None`` at submission.
        fast_path_rationale: Required on fast-path routes; ``None`` when
            routing to the gate.
        gate_context: Gate dispatch envelope. Required on every observation.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    event_id: str
    entity_type: Literal["widget"] = "widget"
    timestamp: datetime
    widget_id: str

    route: GateRoute = GateRoute.ROUTE_TO_GATE
    reasoner_context: ReasonerContext | None = None
    fast_path_rationale: str | None = None
    gate_context: GateContext = GateContext(gate_id="policy")

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """Framework UUID derived deterministically from ``widget_id``."""
        return uuid.uuid5(STUB_WIDGET_NAMESPACE, self.widget_id)
