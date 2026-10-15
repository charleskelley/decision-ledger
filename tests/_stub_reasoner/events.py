"""StubEvent — the stub reasoner's own domain event.

The event the stub "pipeline" decides about. It is the stub's business, not
the framework's: the stub's assembler (``tests/_stub_reasoner/assembler.py``)
turns it into a ``Submission`` and stores it there as opaque ``raw_event``
JSON. ``entity_id`` is a UUID5 derived from ``widget_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field

#: UUID5 namespace for stub widget identities. Distinct from every real
#: reasoner's namespace so stub and production entity ids can never collide.
STUB_WIDGET_NAMESPACE: UUID = UUID("3f6e1b7c-9d2a-5e48-8b1f-0c7a4d9e2b61")


class StubEvent(BaseModel):
    """A widget event the stub reasoner decides about.

    Attributes:
        event_id: Unique identifier for this event instance.
        entity_type: Framework subject classification. Always ``"widget"``.
        timestamp: When the widget was observed (UTC).
        widget_id: Stub domain business key; ``entity_id`` is derived from it.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    event_id: str
    entity_type: Literal["widget"] = "widget"
    timestamp: datetime
    widget_id: str

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """Framework UUID derived deterministically from ``widget_id``."""
        return uuid.uuid5(STUB_WIDGET_NAMESPACE, self.widget_id)
