"""ReasonerRegistration and ReasonerRegistry — framework registration contracts.

A ReasonerRegistration is the framework's declaration of a known domain
reasoner: its stable identifier, display name, and the set of resources
(prompt templates, jurisdictions) it is authorized to use.

A ReasonerRegistry is the protocol any registry implementation must satisfy.
The reference implementation (StaticReasonerRegistry in app/) loads from a
dict at startup. A production implementation could back this with a database
or secrets manager — the framework is agnostic to the backing store.

Registration is a startup-time configuration concern. The framework validates
that an incoming Observation's reasoner_id is registered and that its
gate_context references only resources the registration permits.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ReasonerRegistration(BaseModel):
    """Framework declaration of a known domain reasoner.

    Submitted at startup to declare a reasoner to the DecisionLedger
    framework. The framework uses this record during Observation intake to
    verify that the submitting reasoner is known and that its gate_context
    references only the resources it is authorized to use.

    Args:
        reasoner_id: Stable machine identifier. Must match
            ``ReasonerContext.reasoner_id`` on every Observation this
            reasoner submits. Used as the registry lookup key and as the
            stable cross-bundle query key.
        reasoner_name: Human-readable display name for dashboards and
            audit reports (e.g., ``"ATO Reasoner"``).
        allowed_prompt_template_ids: Prompt templates this reasoner is
            authorized to reference in ``GateContext.prompt_template_id``.
            The framework rejects any Observation whose template_id is
            not in this set. Must be non-empty.
        allowed_jurisdictions: Regulatory jurisdictions this reasoner is
            authorized to include in ``GateContext.jurisdictions``. The
            framework rejects any Observation whose jurisdictions list
            contains a value outside this set. An empty frozenset means
            no jurisdiction restriction is configured for this reasoner.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    reasoner_id: str
    reasoner_name: str
    allowed_prompt_template_ids: frozenset[str]
    allowed_jurisdictions: frozenset[str]


@runtime_checkable
class ReasonerRegistry(Protocol):
    """Protocol for reasoner registries the framework depends on at intake.

    Any object that provides ``get`` and ``is_registered`` satisfies this
    protocol. Domain reasoners are not required to interact with the registry
    directly — it is a framework-side concern used by ``validate_observation``.

    The reference implementation (``StaticReasonerRegistry`` in ``app/``)
    loads from a dict at startup. A production implementation could back
    this with a database or remote config service.
    """

    def get(self, reasoner_id: str) -> ReasonerRegistration:
        """Return the registration for the given reasoner_id.

        Args:
            reasoner_id: The stable reasoner identifier to look up.

        Returns:
            The ``ReasonerRegistration`` for this reasoner.

        Raises:
            UnregisteredReasonerError: If ``reasoner_id`` is not registered.
        """
        ...

    def is_registered(self, reasoner_id: str) -> bool:
        """Return True if reasoner_id has a registration entry.

        Args:
            reasoner_id: The stable reasoner identifier to check.

        Returns:
            ``True`` if registered, ``False`` otherwise.
        """
        ...


__all__ = [
    "ReasonerRegistration",
    "ReasonerRegistry",
]
