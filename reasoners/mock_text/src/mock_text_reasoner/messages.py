"""CustomerMessage — one short text the text mock classifies.

A customer's message to an imaginary bank's automated assistant, as the
assistant's inbox delivers it. It is the mock's business, not the
framework's: the assembler (``mock_text_reasoner.assembler``) turns it into
a ``Submission`` and stores it there as opaque ``raw_event`` JSON.

The customer is the mock's native subject: ``(mock-text-customer,
customer_id)`` (``native_subject``), which the ledger resolves to the
decision's subject (DR-32). ``entity_id`` derives from ``customer_id`` under
the namespace the registration declares
(``registry.MOCK_TEXT_CUSTOMER_NAMESPACE``), so every message from one
customer is about the same ledger subject.
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID  # noqa: TC003 (pydantic resolves the computed field's type)

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from core.identity import NativeSubject
from core.submission import storage_problem
from mock_text_reasoner.registry import MOCK_TEXT_CUSTOMER_NAMESPACE

#: The longest message the mock accepts, in characters.
MAX_TEXT_LENGTH = 2000


class CustomerMessage(BaseModel):
    """One customer message to the automated assistant.

    Attributes:
        event_id: Unique identifier of this message (the decision's
            ``event_id``).
        received_at: When the assistant received it (timezone-aware).
        customer_id: The customer's business key: the key of the message's
            ``native_subject``, from which ``entity_id`` is derived.
        channel: Where the customer wrote it.
        text: What the customer wrote (at most ``MAX_TEXT_LENGTH``
            characters, not blank).
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    event_id: str = Field(min_length=1)
    received_at: AwareDatetime
    customer_id: str = Field(min_length=1)
    channel: Literal["chat", "email", "app"] = "chat"
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)

    @model_validator(mode="after")
    def _readable(self) -> CustomerMessage:
        """The text says something, and every value is one the ledger can store."""
        if not self.text.strip():
            msg = "the message text is blank"
            raise ValueError(msg)
        problem = storage_problem(self.model_dump(mode="json"))
        if problem is not None:
            msg = f"the message holds {problem}, which the ledger cannot store"
            raise ValueError(msg)
        return self

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """The mock's UUID for the customer, derived from ``customer_id`` (DR-32)."""
        return MOCK_TEXT_CUSTOMER_NAMESPACE.entity_id_of(self.customer_id)

    @property
    def entity_type(self) -> str:
        """The ledger subject's type."""
        return MOCK_TEXT_CUSTOMER_NAMESPACE.entity_type

    @property
    def native_subject(self) -> NativeSubject:
        """The customer as the mock names them.

        ``(mock-text-customer, customer_id)``: the customer's own id is the
        subject's key in the mock's namespace.
        """
        return NativeSubject(
            native_namespace=MOCK_TEXT_CUSTOMER_NAMESPACE.native_namespace,
            native_subject_id=self.customer_id,
        )

    def payload(self) -> dict[str, Any]:
        """The message as the inbox delivers it: JSON, without the derived id."""
        return self.model_dump(mode="json", exclude={"entity_id"})

    @classmethod
    def from_payload(cls, payload: object) -> CustomerMessage:
        """Validate one message as the inbox delivers it (JSON-shaped data).

        Validated through JSON, as the wire models are, so ``received_at``
        arrives as an ISO 8601 string and nothing is coerced beyond what JSON
        itself allows.

        Args:
            payload: The message as read.

        Returns:
            The message.

        Raises:
            pydantic.ValidationError: ``payload`` is not a customer message.
            TypeError: ``payload`` is not JSON-serializable.
        """
        return cls.model_validate_json(json.dumps(payload))
