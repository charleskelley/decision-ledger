"""AccountSettlement — one row of the daily mock's settlement reconciliation.

One account's settlement for a business day, as the overnight
reconciliation extract carries it: the amount the bank's books expect to
settle to the account (``target_amount``) and the amount the settlement file
reports it settled (``actual_amount``). It is the mock's business, not the
framework's: the assembler (``mock_daily_reasoner.assembler``) turns it into
a ``Submission`` and stores it there as opaque ``raw_event`` JSON.

The account is the mock's native subject: ``(mock-daily-account,
account_id)`` (``native_subject``), which the ledger resolves to the
decision's subject (DR-32). ``entity_id`` derives from ``account_id`` under
the namespace the registration declares
(``registry.MOCK_DAILY_ACCOUNT_NAMESPACE``), so every day's row for one
account is about the same ledger subject.
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
from mock_daily_reasoner.registry import MOCK_DAILY_ACCOUNT_NAMESPACE


class AccountSettlement(BaseModel):
    """One account's settlement for one business day.

    Attributes:
        event_id: Unique identifier of this row (the decision's ``event_id``).
        as_of: When the reconciliation was taken (timezone-aware; the end of
            the business day it reconciles).
        account_id: The account's business key: the key of the row's
            ``native_subject``, from which ``entity_id`` is derived.
        target_amount: What the books expect to settle to the account that
            day (the target); positive.
        actual_amount: What the settlement file reports was settled (the
            actual).
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    event_id: str = Field(min_length=1)
    as_of: AwareDatetime
    account_id: str = Field(min_length=1)
    target_amount: float = Field(gt=0.0)
    actual_amount: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _storable(self) -> AccountSettlement:
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
        return MOCK_DAILY_ACCOUNT_NAMESPACE.entity_id_of(self.account_id)

    @property
    def entity_type(self) -> str:
        """The ledger subject's type."""
        return MOCK_DAILY_ACCOUNT_NAMESPACE.entity_type

    @property
    def native_subject(self) -> NativeSubject:
        """The account as the mock names it: ``(mock-daily-account, account_id)``."""
        return NativeSubject(
            native_namespace=MOCK_DAILY_ACCOUNT_NAMESPACE.native_namespace,
            native_subject_id=self.account_id,
        )

    def payload(self) -> dict[str, Any]:
        """The row as the extract carries it: JSON, without the derived id."""
        return self.model_dump(mode="json", exclude={"entity_id"})

    @classmethod
    def from_payload(cls, payload: object) -> AccountSettlement:
        """Validate one row as an extract delivers it (JSON-shaped data).

        Validated through JSON, as the wire models are, so ``as_of`` arrives
        as an ISO 8601 string and nothing is coerced beyond what JSON itself
        allows.

        Args:
            payload: The row as read.

        Returns:
            The row.

        Raises:
            pydantic.ValidationError: ``payload`` is not a settlement row.
            TypeError: ``payload`` is not JSON-serializable.
        """
        return cls.model_validate_json(json.dumps(payload))
