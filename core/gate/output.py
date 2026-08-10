"""GateOutput — universal output contract for a gate invocation.

A ``GateOutput`` (or subclass) exists exactly when a gate ran. The base
class carries only what's truly universal: the gate identity and the
typed verdict the framework consumes for enforcement.

``GateOutput`` itself encodes two outcomes:

  * ``verdict`` is a ``GateVerdict``: the gate ran and its response
    validated against the kind's verdict schema. Enforcement reads
    ``verdict`` to produce ``decision_action``.
  * ``verdict is None``: the gate ran but its response failed
    validation. Enforcement routes to HOLD via the schema-failure tier
    (DR-19). Per-kind subclasses carry the forensic evidence of what
    the gate actually emitted (e.g., ``PolicyGateOutput.response_text``
    holds the raw LLM string for HOLD-review investigation).

The third decision-bundle state — *gate not invoked* — is signaled at
the bundle level by ``DecisionBundle.gate_output is None`` (e.g., on the
fast path). That's a bundle concern, not a ``GateOutput`` state; this
type does not represent the absence of a gate run.

Gate-implementation-specific output artifacts (raw text response, token
cost, structured tool-call payload, rule-evaluation trace, etc.) live on
per-gate-type subclasses in their own subpackage — see
``core/gate/policy/output.py`` for the reference LLM policy gate's
``PolicyGateOutput``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from core.gate.verdict import GateVerdict


class GateOutput(BaseModel):
    """Universal output contract for a gate invocation.

    Attributes:
        gate_id: Identifier of the gate that ran. Subclasses narrow this
            to ``Literal[...]`` for Pydantic discriminated-union
            deserialization.
        verdict: The validated typed verdict the framework consumes for
            enforcement. ``None`` when the gate's response failed schema
            validation; in that case enforcement routes to HOLD via the
            schema-failure tier and subclass-specific forensic fields
            carry the evidence.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: str
    verdict: GateVerdict | None
