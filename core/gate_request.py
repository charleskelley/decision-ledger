"""GateRequest — what the framework hands a gate when it dispatches to it.

Lives at the top level of ``core/`` (like ``bundle.py``) because it composes
both subpackages: it carries the intake ``Observation`` and its
``GateContext`` from ``core/observation/`` into a gate, whose output contract
lives in ``core/gate/``. DR-11 forbids ``core/gate/`` from importing
``core/observation/``, so a cross-cutting request type cannot live inside
either subpackage.

The request is the gate's entire view of a decision. A gate reads the
reasoner's evidence (``observation.reasoner_context``) and its own
configuration (``gate_context.gate_config``, already validated against the
gate's declared ``config_model`` at intake) and returns a ``GateOutput``.
Gates are independent: a request never carries another gate's output.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core.observation import GateContext, Observation


class GateRequest(BaseModel):
    """One gate invocation's input, built by the framework.

    Attributes:
        decision_id: The decision being made; gates use it for log
            correlation only.
        observation: The validated intake ``Observation``. Its
            ``reasoner_context`` is guaranteed populated by intake
            validation.
        gate_context: The dispatch envelope addressed to this gate. In the
            current single-gate contract it is ``observation.gate_context``.
        budget_ms: Wall-clock budget the framework grants this invocation,
            in milliseconds. Gates should plan their own internal timeouts
            within it. The framework does not yet enforce it; timeout
            handling (a timed-out gate fails closed) arrives with the gate
            set and combinator (DR-29).
    """

    model_config = ConfigDict(strict=True, frozen=True, arbitrary_types_allowed=True)

    decision_id: str
    observation: Observation
    gate_context: GateContext
    budget_ms: int = Field(gt=0)
