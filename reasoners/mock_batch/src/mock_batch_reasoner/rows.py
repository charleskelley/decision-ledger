"""AccountDay — one row of the batch mock's daily review.

One account's end-of-day position, as an overnight extract carries it. It is
the mock's business, not the framework's: the assembler
(``mock_batch_reasoner.assembler``) turns it into a ``Submission`` and
stores it there as opaque ``raw_event`` JSON.

The account is the mock's native subject: ``(mock-batch-account,
account_id)`` (``native_subject``), which the ledger resolves to the
decision's subject (DR-32). ``entity_id`` derives from ``account_id`` under
the namespace the registration declares
(``registry.MOCK_BATCH_ACCOUNT_NAMESPACE``), so every day's row for one
account is about the same ledger subject.

``needs_review`` is set upstream, outside the scorer (an open dispute, a
manual flag): no bypass rule covers a flagged row, so it is one of the
batch's exceptions, decided by the rules gate.
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
from mock_batch_reasoner.registry import MOCK_BATCH_ACCOUNT_NAMESPACE


class AccountDay(BaseModel):
    """One account's end-of-day position.

    Attributes:
        event_id: Unique identifier of this row (the decision's ``event_id``).
        as_of: When the position was taken (timezone-aware; the end of the
            business day the batch reviews).
        account_id: The account's business key: the key of the row's
            ``native_subject``, from which ``entity_id`` is derived.
        balance: The amount owed on the account.
        credit_limit: The account's credit limit.
        days_past_due: Days since the oldest unpaid minimum payment was due.
        returned_payments: Payments returned unpaid in the last 30 days.
        needs_review: The account is flagged for review upstream; no bypass
            rule covers it.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    event_id: str = Field(min_length=1)
    as_of: AwareDatetime
    account_id: str = Field(min_length=1)
    balance: float = Field(ge=0.0)
    credit_limit: float = Field(gt=0.0)
    days_past_due: int = Field(ge=0)
    returned_payments: int = Field(ge=0)
    needs_review: bool

    @model_validator(mode="after")
    def _storable(self) -> AccountDay:
        """Every value is one the ledger can store (the submission carries it)."""
        problem = storage_problem(self.model_dump(mode="json"))
        if problem is not None:
            msg = f"the row holds {problem}, which the ledger cannot store"
            raise ValueError(msg)
        return self

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """The mock's UUID for the account, derived from ``account_id`` (DR-32)."""
        return MOCK_BATCH_ACCOUNT_NAMESPACE.entity_id_of(self.account_id)

    @property
    def entity_type(self) -> str:
        """The ledger subject's type."""
        return MOCK_BATCH_ACCOUNT_NAMESPACE.entity_type

    @property
    def native_subject(self) -> NativeSubject:
        """The account as the mock names it: ``(mock-batch-account, account_id)``."""
        return NativeSubject(
            native_namespace=MOCK_BATCH_ACCOUNT_NAMESPACE.native_namespace,
            native_subject_id=self.account_id,
        )

    def payload(self) -> dict[str, Any]:
        """The row as the extract carries it: JSON, without the derived id."""
        return self.model_dump(mode="json", exclude={"entity_id"})

    @classmethod
    def from_payload(cls, payload: object) -> AccountDay:
        """Validate one row as an extract delivers it (JSON-shaped data).

        Validated through JSON, as the wire models are, so ``as_of`` arrives
        as an ISO 8601 string and nothing is coerced beyond what JSON itself
        allows.

        Args:
            payload: The row as read.

        Returns:
            The row.

        Raises:
            pydantic.ValidationError: ``payload`` is not an account-day row.
            TypeError: ``payload`` is not JSON-serializable.
        """
        return cls.model_validate_json(json.dumps(payload))
