"""TransferEvent — the stream mock's own domain event.

One transfer an imaginary account sends, as it arrives on the stream. It is
the mock's business, not the framework's: the assembler
(``mock_stream_reasoner.assembler``) turns it into a ``Submission`` and
stores it there as opaque ``raw_event`` JSON.

The account is the mock's native subject: ``(mock-stream-account,
account_id)`` (``native_subject``), which the ledger resolves to the
decision's subject (DR-32). ``entity_id`` derives from ``account_id`` under
the namespace the registration declares
(``registry.MOCK_STREAM_ACCOUNT_NAMESPACE``), so every transfer of one account
is about the same ledger subject.
"""

from __future__ import annotations

import json
from typing import Any
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
from mock_stream_reasoner.registry import MOCK_STREAM_ACCOUNT_NAMESPACE


class TransferEvent(BaseModel):
    """One transfer, as the stream carries it.

    Attributes:
        event_id: Unique identifier of this event; the source drops a second
            delivery of an id it has seen.
        timestamp: When the transfer happened (timezone-aware event time; the
            per-account windows and the source's lateness check read it, not
            the arrival order).
        account_id: The sending account's business key: the key of the
            event's ``native_subject``, from which ``entity_id`` is derived.
        amount: The amount sent, in an imaginary currency.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    event_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    account_id: str = Field(min_length=1)
    amount: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _storable(self) -> TransferEvent:
        """Every value is one the ledger can store (the submission carries it)."""
        problem = storage_problem(self.model_dump(mode="json"))
        if problem is not None:
            msg = f"the event holds {problem}, which the ledger cannot store"
            raise ValueError(msg)
        return self

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """The mock's UUID for the account, derived from ``account_id`` (DR-32)."""
        return MOCK_STREAM_ACCOUNT_NAMESPACE.entity_id_of(self.account_id)

    @property
    def entity_type(self) -> str:
        """The ledger subject's type."""
        return MOCK_STREAM_ACCOUNT_NAMESPACE.entity_type

    @property
    def native_subject(self) -> NativeSubject:
        """The account as the mock names it: ``(mock-stream-account, account_id)``."""
        return NativeSubject(
            native_namespace=MOCK_STREAM_ACCOUNT_NAMESPACE.native_namespace,
            native_subject_id=self.account_id,
        )

    def payload(self) -> dict[str, Any]:
        """The event as the stream carries it: JSON, without the derived id."""
        return self.model_dump(mode="json", exclude={"entity_id"})

    @classmethod
    def from_payload(cls, payload: object) -> TransferEvent:
        """Validate one event as the stream delivers it (JSON-shaped data).

        Validated through JSON, as the wire models are, so a timestamp
        arrives as an ISO 8601 string and nothing is coerced beyond what JSON
        itself allows.

        Args:
            payload: The event as received.

        Returns:
            The event.

        Raises:
            pydantic.ValidationError: ``payload`` is not a transfer event.
            TypeError: ``payload`` is not JSON-serializable.
        """
        return cls.model_validate_json(json.dumps(payload))
