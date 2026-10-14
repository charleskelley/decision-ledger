"""GateInput — the framework's record that it invoked a gate.

The framework writes a ``GateInput`` into the bundle whenever it dispatches
to a gate (``DecisionBundle.gate_input``); it is ``None`` exactly when no
gate ran. The base carries only what is truly universal: the identity of the
gate that was invoked.

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
            ``raw_event.gate_context.gate_id`` (denormalized so the gate
            layer of a bundle is self-describing for audit reading).
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: str
