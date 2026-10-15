"""DecisionBundle (schema version 2) — the complete, replayable decision record.

One bundle per decision, immutable once written. Version 2 (DR-29 to DR-31)
stores the reasoner's ``Submission`` verbatim instead of a reasoner-typed
event, and one ``GateRecord`` per gate in the decision's gate set instead of
a single gate's input and output, so no reasoner code, no gate, and no
corpus is needed to audit or replay a decision. The enforcement profile
replay needs (the novel-entity feature and the jurisdiction conflict
groups) comes from the snapshot of the registration version the bundle
records (``registration_version``, DR-31), which the ledger keeps for every
version it serves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.actions import DecisionAction
from core.enforcement import EnforcementDecision
from core.gate import GateInput, GateOutput, GateStatus
from core.submission import BypassCheck, Submission

#: The bundle schema version this module defines (the
#: ``decision_bundles.schema_version`` column of every row written).
BUNDLE_SCHEMA_VERSION = 2


class GateRecord(BaseModel):
    """What happened at one gate of a decision's gate set.

    Field semantics by ``status``:

    * ``ran`` — the framework invoked the gate (``input``) and it returned a
      valid universal output (``output``); ``enforcement`` holds the
      gate-level tiers applied to it.
    * ``failed`` — the gate produced no usable output: it raised, timed out,
      returned something other than a plain ``GateOutput`` for itself, or no
      gate is composed under its id. ``output`` is ``None``, ``reason`` says
      why, ``input`` is set when the framework did invoke it, and
      ``enforcement`` holds the fail-closed result (the schema-failure tier:
      HOLD).
    * ``skipped`` — the framework did not invoke the gate: an advisory gate
      on a recorded decision, or any advisory gate once the decision was
      already BLOCK. ``reason`` says which; nothing else is set.

    Attributes:
        gate_id: The gate.
        mandatory: Whether the gate was mandatory for this decision (bound
            mandatory by the registration or mandated by the deployment).
        status: ``ran`` | ``skipped`` | ``failed``.
        reason: Why the gate was skipped or failed; ``None`` when it ran.
        input: The framework's record that it invoked the gate.
        output: The gate's universal output (opaque extension included).
        enforcement: Gate-level enforcement of this gate's output.
        duration_ms: Wall-clock time of the gate call, or ``None`` when the
            gate was not invoked.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    gate_id: str
    mandatory: bool
    status: GateStatus
    reason: str | None = None
    input: GateInput | None = None
    output: GateOutput | None = None
    enforcement: EnforcementDecision | None = None
    duration_ms: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _fields_match_status(self) -> GateRecord:
        """Each status carries exactly the fields its semantics define."""
        problem: str | None = None
        if self.status == "ran":
            if self.input is None or self.output is None or self.enforcement is None:
                problem = "a gate that ran records its input, output, and enforcement"
            elif {self.output.gate_id, self.input.gate_id} != {self.gate_id}:
                problem = "the input and output of a gate record belong to its gate"
        elif self.status == "failed":
            if self.output is not None or self.enforcement is None or not self.reason:
                problem = (
                    "a failed gate records no output, a reason, and its "
                    "fail-closed enforcement"
                )
        elif (
            self.input is not None
            or self.output is not None
            or self.enforcement is not None
            or not self.reason
        ):
            problem = "a skipped gate records only why it was skipped"
        if problem is not None:
            msg = f"gate record {self.gate_id!r} ({self.status}): {problem}"
            raise ValueError(msg)
        return self

    @property
    def enforced_action(self) -> DecisionAction | None:
        """The gate-level enforced action, or ``None`` for a skipped gate."""
        return self.enforcement.decision_action if self.enforcement else None


class DecisionBundle(BaseModel):
    """Complete, replayable audit record for every decision (schema version 2).

    Stored in PostgreSQL (``decisionledger.decision_bundles``), indexed by
    ``decision_id``. Every field is logged at decision time — nothing is
    computed on read, and the row is never updated once written.

    Replay guarantee: given a bundle and its reasoner's enforcement profile
    (the registration's novel-entity feature and jurisdiction conflict
    groups), re-running the gate-level tiers on each stored gate output,
    re-evaluating the stored bypass rules against the stored submission's
    evidence, and re-running the decision-level rules and the combinator
    produce the same ``decision_action``. Replay does **not** re-invoke any
    gate, reads no corpus, and needs no reasoner code.

    Action vocabulary:
        * ``decision_action`` — the action this bundle produced at decision
          time. Immutable.
        * ``resolution_action`` — for non-terminal decisions (CHALLENGE,
          HOLD), the action a resolver later produced. Lives on
          ``core.resolution.ResolutionAttempt`` rows; never on the bundle.
        * ``realized_action`` — the action ultimately taken on the entity,
          computed from the bundle plus the resolution attempt log; never
          stored.

    Attributes:
        schema_version: Always ``2``.
        decision_id: UUID of this decision record.
        created_at: When the bundle was constructed (UTC).
        submission: The reasoner's submission, verbatim — identity, mode,
            proposal and citation, evidence, gate contexts, and its opaque
            ``raw_event``.
        idempotency_key: Deduplication key (the event id unless the caller
            supplied one).
        ingestion_timestamp: When the ledger received the submission (over
            HTTP or through a client; the event's own time is
            ``submission.timestamp``).
        gates: One record per gate in the decision's gate set, mandatory
            gates first, each in gate-set order.
        decision_action: The action this decision produced.
        decision_level_rule: The decision-level rule that fired, if any
            (not necessarily the one that set the final action — a mandatory
            gate may still be stricter; the combinator line says what did):
            ``tier3_novel_entity`` (a gated decision about a novel entity,
            its advisory verdicts held for review, unless an advisory gate's
            own Tier 1 or 2 outranked it), ``bypass_rule_not_satisfied`` (a
            record whose cited rule did not hold: HOLD floor), or
            ``bypass_rule_superseded`` (a record citing a rule while another
            registered rule that also holds justifies a stricter action:
            floor at that action).
        override_log: The decision-level trail — the bypass check or the
            novel-entity tier, then the combinator line. Each gate's own
            tier log is in its ``GateRecord.enforcement``.
        bypass_rule_holds: For a record, whether the cited rule held
            against the submission's evidence; ``None`` for a gated
            decision. Equals ``bypass_check.holds``.
        bypass_check: For a record, the cited rule as evaluated (snapshot)
            and the values it read; ``None`` for a gated decision.
        other_bypass_checks: For a record, every other rule the reasoner
            registers, as evaluated (snapshots), so replay can re-check that
            no stricter rule also held; empty for a gated decision.
        registration_version: The version (content address) of the
            reasoner's registration the decision was made under (DR-31).
            Replay enforces against that version's snapshot, served at
            ``GET /api/v1/reasoners/{reasoner_id}/registrations/{version}``.
            ``None`` only on a bundle written before registrations were
            versioned.
        latency_breakdown: The ledger's own steps in milliseconds:
            ``gate_ms`` (wall-clock time of the whole gate phase; each
            gate's own time is in its record) and ``enforcement_ms``, plus
            any step an in-process caller of ``submit`` reported. A reasoner
            reports its own timings itself.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    schema_version: Literal[2] = 2
    decision_id: str
    created_at: datetime
    submission: Submission
    idempotency_key: str
    ingestion_timestamp: datetime
    gates: tuple[GateRecord, ...]
    decision_action: DecisionAction
    decision_level_rule: str | None
    override_log: tuple[str, ...]
    bypass_rule_holds: bool | None
    bypass_check: BypassCheck | None
    other_bypass_checks: tuple[BypassCheck, ...] = ()
    registration_version: str | None = None
    latency_breakdown: dict[str, float]

    @model_validator(mode="after")
    def _record_layer_matches_mode(self) -> DecisionBundle:
        """A record carries its bypass check; a gated decision carries none."""
        recorded = self.submission.mode == "record"
        if recorded != (self.bypass_check is not None):
            msg = "bypass_check is set exactly when the submission is a record"
            raise ValueError(msg)
        expected = self.bypass_check.holds if self.bypass_check is not None else None
        if self.bypass_rule_holds != expected:
            msg = "bypass_rule_holds must equal bypass_check.holds (None when gated)"
            raise ValueError(msg)
        if self.other_bypass_checks and not recorded:
            msg = "other_bypass_checks are recorded for a record only"
            raise ValueError(msg)
        gate_ids = [record.gate_id for record in self.gates]
        if len(gate_ids) != len(set(gate_ids)):
            msg = f"a gate appears more than once in the gate records: {gate_ids}"
            raise ValueError(msg)
        return self

    def gate(self, gate_id: str) -> GateRecord | None:
        """Return the record of ``gate_id``, or ``None`` if it was not in the set."""
        return next((r for r in self.gates if r.gate_id == gate_id), None)
