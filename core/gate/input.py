"""GateInput — universal input contract for a gate invocation.

Every gate populates a ``GateInput`` (or a subclass) at invocation time.
The base class carries only what's truly universal: the gate identity.

Gate-implementation-specific invocation context (rendered prompt,
template metadata, model version, retrieval corpus version, rule-engine
version, etc.) lives on per-gate-type subclasses in their own subpackage
— see ``core/gate/policy/input.py`` for the reference LLM policy gate's
``PolicyGateInput``.

Discrimination across subclasses is by ``gate_id`` (Pydantic discriminator
narrowed to ``Literal[...]`` in each subclass).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GateInput(BaseModel):
    """Universal input contract for a gate invocation.

    Attributes:
        gate_id: Identifier of the gate that ran. Subclasses narrow this
            to ``Literal[...]`` for Pydantic discriminated-union
            deserialization. Mirrors ``raw_event.gate_context.gate_id``
            (denormalized here so a ``GateInput`` is self-contained for
            audit reading).
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: str
