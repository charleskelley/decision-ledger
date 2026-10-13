"""GateOutput and EvidenceRef — the open, universal output contract of a gate.

A ``GateOutput`` exists exactly when a gate ran. Per DR-28 the contract is
*open*: the framework types and interprets a small universal base and stores
everything gate-specific as opaque JSON it never reads.

Universal base (what enforcement and replay consume):

  * ``gate_id`` — which gate produced the output.
  * ``deterministic`` — whether the gate is a pure function of its input
    (rules, lookups). Enforcement skips the adversarial-probe and
    low-confidence tiers for deterministic gates (DR-28).
  * ``verdict`` — the typed ``GateVerdict``; ``None`` means the gate ran but
    produced no valid verdict, which enforcement routes to HOLD through the
    schema-failure tier (DR-19).
  * ``evidence`` — references to the sources the gate relied on. The
    jurisdiction-conflict tier reads their ``jurisdictions`` and runs only
    when at least one reference carries any.

Opaque extension (gate-owned; stored verbatim, never interpreted):

  * ``evidence_snapshot`` — everything the gate consulted to reach its
    verdict (for a retrieving gate: the query, the retrieved passages, the
    rendered prompt). It makes the bundle self-contained for audit and
    replay; replay never re-invokes the gate or reads a corpus.
  * ``extra`` — gate-specific output fields (a raw model response, token
    cost, a rationale, rule traces).

A gate may keep typed models for its own extension and serialize them into
these fields with ``model_dump(mode="json")``; readers that know the gate
re-validate them. The framework never defines, imports, or deserializes a
``GateOutput`` subclass — it persists and replays this base only.

The bundle-level *gate not invoked* state (``DecisionBundle.gate_output is
None``, e.g. on the fast path) is a bundle concern, not a ``GateOutput``
state.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from core.gate.verdict import GateVerdict


class EvidenceRef(BaseModel):
    """A reference to one source a gate relied on (generalizes a citation).

    Attributes:
        source_id: Stable identifier of the source (a policy document id, a
            rule-set id, a lookup table name).
        locator: Optional position inside the source (a section path, a
            rule id, a row key). ``None`` when the source is atomic.
        jurisdictions: Regulatory jurisdiction tags the source carries.
            Empty for sources that have no jurisdiction. Enforcement's
            jurisdiction-conflict tier reads the union across a gate's
            evidence and checks it against the reasoner's conflict groups.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    source_id: str
    locator: str | None = None
    jurisdictions: frozenset[str] = frozenset()


class GateOutput(BaseModel):
    """Universal output contract for a gate invocation (open per DR-28).

    Attributes:
        gate_id: Identifier of the gate that ran. Equals the
            ``GateContext.gate_id`` the framework dispatched on.
        deterministic: ``True`` when the gate is a pure function of its
            input (the plugin's declared value, recorded here so replay
            applies the same tiers). Deterministic gates skip enforcement
            Tiers 2 (adversarial probe) and 4 (low confidence).
        verdict: The validated verdict enforcement consumes. ``None`` when
            the gate produced no valid verdict; enforcement routes to HOLD
            via the schema-failure tier and ``extra`` may carry forensics.
            When present, ``verdict.gate_id`` equals ``gate_id``.
        evidence: References to the sources the gate relied on. Enforcement
            Tier 5 (jurisdiction conflict) runs only when at least one
            reference carries jurisdictions.
        evidence_snapshot: Opaque, gate-owned JSON describing what the gate
            consulted, stored verbatim for audit and replay. ``None`` when
            the gate has nothing to record.
        extra: Opaque, gate-specific output fields (JSON values). The
            framework stores them without interpretation.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: str
    deterministic: bool
    verdict: GateVerdict | None
    evidence: tuple[EvidenceRef, ...] = ()
    evidence_snapshot: JsonValue | None = None
    extra: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _verdict_belongs_to_this_gate(self) -> GateOutput:
        """A verdict is attributed to the gate whose output carries it."""
        if self.verdict is not None and self.verdict.gate_id != self.gate_id:
            msg = (
                f"verdict.gate_id {self.verdict.gate_id!r} does not match "
                f"gate_id {self.gate_id!r}"
            )
            raise ValueError(msg)
        return self

    def evidence_jurisdictions(self) -> frozenset[str]:
        """Return the union of jurisdiction tags across ``evidence``."""
        return frozenset().union(*(ref.jurisdictions for ref in self.evidence))
