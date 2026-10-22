"""GateRequest — what the framework hands a gate when it dispatches to it.

Lives at the top level of ``core/`` (like ``bundle.py``) because it composes
both subpackages' contracts: it carries the reasoner's ``Submission`` (built
from ``core/observation/`` types) and the ``GateContext`` addressed to one
gate into that gate, whose output contract lives in ``core/gate/``. DR-11
forbids ``core/gate/`` from importing ``core/observation/``, so a
cross-cutting request type cannot live inside either subpackage.

The request is the gate's entire view of a decision. A gate reads the
reasoner's evidence (``submission.reasoner_context``), the decision's
subject as the ledger resolved it (``identity``, DR-32: a lookup gate keyed
by subject reads its id here), its configuration for this decision
(``gate_context.gate_config``, already validated against the gate's
declared ``config_model`` at intake), and its configuration for this
reasoner (``binding_config``, the binding's config in the registration the
decision is made under, which the gate admitted when that registration was
composed or written) and returns a ``GateOutput``.
Gates are independent (DR-29): a request never carries another gate's
output, and every gate in a decision's gate set sees the same submission.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from core.identity import ResolvedIdentity
from core.observation import GateContext
from core.submission import Submission


class GateRequest(BaseModel):
    """One gate invocation's input, built by the framework.

    Attributes:
        decision_id: The decision being made; gates use it for log
            correlation only.
        submission: The validated submission. Gates read its
            ``reasoner_context``; its ``raw_event`` is the reasoner's own
            and opaque.
        gate_context: The dispatch envelope addressed to this gate: the
            submission's context for it, or an empty one for a gate the
            deployment mandates that the reasoner does not bind.
        budget_ms: Wall-clock budget the framework grants this invocation,
            in milliseconds, and enforces: a gate that has not returned
            within it is cancelled and fails closed. Gates should plan their
            own internal timeouts within it.
        identity: The decision's subject, resolved by the ledger before any
            gate runs (DR-32): the subject a gate about the person (a
            do-not-contact list, a sanctions screen) should key on, rather
            than the reasoner's own ``submission.entity_id``. Always set on
            a live decision; ``None`` only when a bundle written before the
            identity contract is evaluated again (shadow evaluation).
        binding_config: The ``config`` of the reasoner's binding to this
            gate, from the registration the gate runs under: on a live
            decision, the version the bundle cites (whose snapshot keeps
            it); in shadow evaluation, the one served now (the decision is
            made again under today's registration). The gate validated and
            admitted it when that registration was composed or written
            (DR-33); it reads it here on every invocation rather than
            keeping its own copy. Empty for a binding without configuration
            and for a gate the deployment mandates that the reasoner does
            not bind.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    decision_id: str
    submission: Submission
    gate_context: GateContext
    budget_ms: int = Field(gt=0)
    identity: ResolvedIdentity | None = None
    binding_config: dict[str, JsonValue] = Field(default_factory=dict)
