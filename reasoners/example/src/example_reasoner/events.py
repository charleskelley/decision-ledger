"""WidgetEvent — the example reasoner's own domain event.

The event the example decides about. It is the reasoner's business, not the
framework's: the assembler (``example_reasoner.assembler``) turns it into a
``Submission`` and stores it there as opaque ``raw_event`` JSON. ``entity_id``
is a UUID5 derived from ``widget_id``, so every inspection of one widget is
about the same ledger subject.
"""

from __future__ import annotations

import uuid
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field

#: UUID5 namespace for example widget identities. Distinct from every other
#: reasoner's namespace (the stub's included) so entity ids never collide.
EXAMPLE_WIDGET_NAMESPACE: UUID = UUID("8b4f2a6e-1c3d-5e7f-9a0b-2c4d6e8f0a1b")

#: The regions a widget is inspected in. Each has its own policy document.
Region = Literal["REGION_A", "REGION_B"]


class WidgetEvent(BaseModel):
    """One inspection of a widget.

    Attributes:
        event_id: Unique identifier of this inspection.
        timestamp: When the widget was inspected (timezone-aware).
        widget_id: The widget's business key; ``entity_id`` is derived from
            it.
        region: Where it was inspected.
        wobble: Measured wobble, 0 (steady) to 1 (shaking apart).
        misalignment_mm: Gizmo misalignment in millimetres.
        prior_inspections: How many earlier inspections the widget has had.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    event_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    widget_id: str = Field(min_length=1)
    region: Region
    wobble: float = Field(ge=0.0, le=1.0)
    misalignment_mm: float = Field(ge=0.0)
    prior_inspections: int = Field(ge=0)

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """Framework UUID derived deterministically from ``widget_id``."""
        return uuid.uuid5(EXAMPLE_WIDGET_NAMESPACE, self.widget_id)

    @property
    def entity_type(self) -> str:
        """The ledger subject's type."""
        return "widget"
