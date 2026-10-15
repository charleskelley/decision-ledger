"""GateInput — the framework's record that it invoked a gate.

The framework writes a ``GateInput`` into a gate's ``GateRecord`` whenever it
dispatches to that gate; it is ``None`` exactly when the gate was not
invoked (skipped, or no gate composed under its id). The base carries only
what is truly universal: the identity of the gate that was invoked.

Everything a gate consulted or constructed while evaluating — a rendered
prompt and its template snapshot, retrieved passages, a model version, a
rule-set version — is gate-owned and lives in that gate's
``GateOutput.evidence_snapshot`` / ``extra`` (DR-28). The framework never
defines or deserializes a ``GateInput`` subclass.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GateInput(BaseModel):
    """The framework's record of one gate invocation.

    Attributes:
        gate_id: Identifier of the gate the framework dispatched to. Mirrors
            the ``GateContext.gate_id`` it dispatched on.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    gate_id: str
