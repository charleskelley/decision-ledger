"""WidgetEvent — the example reasoner's own domain event.

The event the example decides about. It is the reasoner's business, not the
framework's: the assembler (``example_reasoner.assembler``) turns it into a
``Submission`` and stores it there as opaque ``raw_event`` JSON.

The widget is the example's native subject: ``(example-widget, widget_id)``
(``native_subject``), which the ledger resolves to the decision's subject
(DR-32). ``entity_id`` is the example's own id for the widget, derived from
``widget_id`` under the namespace the example's registration declares
(``registry.EXAMPLE_WIDGET_NAMESPACE``), so every inspection of one widget
is about the same ledger subject.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID  # noqa: TC003 (pydantic resolves the computed field's type)

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field

from core.identity import NativeSubject
from example_reasoner.registry import EXAMPLE_WIDGET_NAMESPACE

#: The regions a widget is inspected in. Each has its own policy document.
Region = Literal["REGION_A", "REGION_B"]


class WidgetEvent(BaseModel):
    """One inspection of a widget.

    Attributes:
        event_id: Unique identifier of this inspection.
        timestamp: When the widget was inspected (timezone-aware).
        widget_id: The widget's business key: the key of the event's
            ``native_subject``, from which ``entity_id`` is derived.
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
        """The example's UUID for the widget, derived from ``widget_id`` (DR-32)."""
        return EXAMPLE_WIDGET_NAMESPACE.entity_id_of(self.widget_id)

    @property
    def entity_type(self) -> str:
        """The ledger subject's type."""
        return EXAMPLE_WIDGET_NAMESPACE.entity_type

    @property
    def native_subject(self) -> NativeSubject:
        """The widget as the example names it: ``(example-widget, widget_id)``."""
        return NativeSubject(
            native_namespace=EXAMPLE_WIDGET_NAMESPACE.native_namespace,
            native_subject_id=self.widget_id,
        )
