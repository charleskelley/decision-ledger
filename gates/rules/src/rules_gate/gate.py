"""RulesGate — evaluates a registered rule set against a reasoner's evidence.

A deterministic, zero-infrastructure ``Gate`` (DR-28). For each request it
looks up the rule set the gate context names, checks the rule set belongs to
the submitting reasoner, evaluates every rule's predicate against the
reasoner's ``label_value`` / ``feature_set`` with the framework's pure
predicate evaluator, and returns the universal ``GateOutput``:

- ``verdict`` — permits the actions of every matching rule (enforcement takes
  the most conservative) or the rule set's ``default_action`` when none
  matches; carries the matching rules' required controls; ``confidence``
  1.0. ``None`` when the gate cannot evaluate (unknown rule set, a rule set
  registered for another reasoner, or a predicate that cannot evaluate),
  which enforcement holds via the schema-failure tier.
- ``deterministic`` — ``True``: enforcement skips the adversarial-probe and
  low-confidence tiers.
- ``evidence`` — the matching rules (``source_id`` = ``<ruleset>@<version>``,
  ``locator`` = rule id), or the rule set's default when none matched. Rules
  carry no jurisdictions, so the jurisdiction-conflict tier is skipped. Each
  reference carries the SHA-256 of the rule set's canonical JSON
  (``content_hash``) and, when the gate was built with the ledger's evidence
  escrow, the digest the escrow returned for that JSON (``artifact_hash``,
  DR-34 part 2).
- ``evidence_snapshot`` — a ``RulesGateSnapshot``: the full rule set as
  evaluated, the matching rule ids, and the observed values of every field
  the rules reference, so the decision is auditable without the rule files.
- ``extra`` — ``{"failure": ..., "detail": ...}`` when there is no verdict.

The gate verifies its evidence (``verify``, the framework's
``EvidenceVerifier`` capability): a reference is ``intact`` when the rule set
escrowed for it is the one the snapshot recorded (stored) or when the rule
set's file, read again, still canonicalizes to it (live);
``source_changed`` when the file now holds other rules; ``source_unavailable``
when the file is gone or unreadable; ``not_verifiable`` when nothing was
escrowed or the reference does not match the snapshot. The escrow is the
framework's store, written through the framework's connection; the gate
holds no connection of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from core.exceptions import PredicateError
from core.gate import (
    EvidenceRef,
    EvidenceStatus,
    GateOutput,
    GateVerdict,
    evaluate_predicate,
)
from core.gate.predicate import Scalar, referenced_fields, resolve_field
from rules_gate.ruleset import (
    RulesGateConfig,
    canonical_ruleset,
    read_ruleset,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.gate.predicate import PredicateSubject
    from core.gate_request import GateRequest
    from rules_gate.ruleset import Rule, RuleSet

log = structlog.get_logger(__name__)

#: The rules gate's identifier — what ``GateContext.gate_id`` names.
RULES_GATE_ID = "rules"

#: Why the rules gate produced no verdict.
RulesGateFailure = Literal[
    "unknown_ruleset", "ruleset_reasoner_mismatch", "predicate_error"
]


class EvidenceEscrow(Protocol):
    """The ledger's evidence escrow, as the gate uses it (DR-34 part 2).

    The framework's store satisfies it (``framework.store.artifacts``): the
    plugin takes it when the gate is built.
    """

    def put(
        self, content: bytes, *, media_type: str, decision_id: str, gate_id: str
    ) -> str | None:
        """Escrow ``content``; its digest, or ``None`` when escrow is off."""
        ...

    def get(self, artifact_hash: str) -> bytes | None:
        """The escrowed bytes with that digest, or ``None``."""
        ...


class RulesGateSnapshot(BaseModel):
    """Schema of the rules gate's ``evidence_snapshot``.

    Attributes:
        ruleset_id: The rule set the gate context selected.
        ruleset: The full rule set as evaluated (JSON), so the bundle is
            self-contained even after the rule file changes. ``None`` when
            the rule set id is unknown or the rule set belongs to another
            reasoner (its rules are not copied into this tenant's bundle).
        matched_rule_ids: Ids of the rules whose predicates held, in rule
            order.
        default_applied: ``True`` when no rule matched and the rule set's
            default action was permitted.
        observed: The subject's value for every field address the rule set
            references (fields the subject lacks are omitted).
        ruleset_hash: SHA-256 (lowercase hex) of the rule set's canonical
            JSON as applied, computed when the gate decided: the digest every
            evidence reference names (``content_hash``) and the escrow keys
            the rule set by. Kept as computed because the stored ``ruleset``
            JSON may come back with its numbers rendered differently (jsonb).
            ``None`` when no rule set was applied (and before DR-34 part 2).
    """

    model_config = ConfigDict(strict=True, frozen=True)

    ruleset_id: str
    ruleset: JsonValue | None
    matched_rule_ids: list[str] = Field(default_factory=list)
    default_applied: bool = False
    observed: dict[str, Scalar] = Field(default_factory=dict)
    ruleset_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RulesGate:
    """Deterministic gate over registered rule sets.

    Satisfies ``app.gates.Gate`` and its optional ``EvidenceVerifier``
    capability. Holds no connections; ``close()`` is a no-op.

    Args:
        rulesets: Registered rule sets keyed by ``ruleset_id``.
        origins: The file each rule set was loaded from, by ``ruleset_id``
            (live verify reads it again); a rule set without one verifies
            ``source_unavailable`` live.
        escrow: The ledger's evidence escrow, or ``None`` for a gate built
            without one: it then escrows nothing.
    """

    def __init__(
        self,
        rulesets: Mapping[str, RuleSet],
        *,
        origins: Mapping[str, Path] | None = None,
        escrow: EvidenceEscrow | None = None,
    ) -> None:
        """Bind the registered rule sets, their files, and the escrow."""
        self._rulesets = dict(rulesets)
        self._origins = dict(origins or {})
        self._escrow = escrow

    @property
    def ruleset_ids(self) -> list[str]:
        """The registered rule set ids, sorted."""
        return sorted(self._rulesets)

    @property
    def escrow(self) -> EvidenceEscrow | None:
        """The ledger's evidence escrow the gate writes to, if it was given one."""
        return self._escrow

    def close(self) -> None:
        """Release nothing — the rules gate opens no resources."""

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Evaluate the selected rule set against the reasoner's evidence.

        Args:
            request: Framework-built request. ``gate_context.gate_config``
                satisfies ``RulesGateConfig`` (validated at intake).

        Returns:
            The universal ``GateOutput`` (``gate_id="rules"``,
            ``deterministic=True``).
        """
        config = RulesGateConfig.model_validate(request.gate_context.gate_config or {})
        subject = request.submission.reasoner_context
        log_ctx = {
            "component": "rules_gate",
            "decision_id": request.decision_id,
            "ruleset_id": config.ruleset_id,
        }

        ruleset = self._rulesets.get(config.ruleset_id)
        if ruleset is None:
            return self._failed(
                "unknown_ruleset",
                f"no rule set registered as {config.ruleset_id!r}",
                ruleset_id=config.ruleset_id,
                ruleset=None,
                log_ctx=log_ctx,
            )
        if ruleset.reasoner_id != subject.reasoner_id:
            return self._failed(
                "ruleset_reasoner_mismatch",
                f"rule set {ruleset.ruleset_id!r} is registered for another "
                f"reasoner, not {subject.reasoner_id!r}",
                ruleset_id=ruleset.ruleset_id,
                # Another tenant's rules never land in this tenant's bundle.
                ruleset=None,
                log_ctx=log_ctx,
            )

        observed = _observed_values(ruleset, subject)
        try:
            matched = [
                rule for rule in ruleset.rules if evaluate_predicate(rule.when, subject)
            ]
        except PredicateError as err:
            return self._failed(
                "predicate_error",
                err.reason,
                ruleset_id=ruleset.ruleset_id,
                ruleset=ruleset,
                observed=observed,
                log_ctx=log_ctx,
            )

        permitted = [rule.action for rule in matched] or [ruleset.default_action]
        controls = sorted({c for rule in matched for c in rule.required_controls})
        verdict = GateVerdict(
            gate_id=RULES_GATE_ID,
            permitted_actions=permitted,
            required_controls=controls,
            confidence=1.0,
            escalate_to_human=False,
            escalation_reason=None,
        )
        applied = canonical_ruleset(ruleset)
        digest = hashlib.sha256(applied).hexdigest()
        snapshot = RulesGateSnapshot(
            ruleset_id=ruleset.ruleset_id,
            ruleset=ruleset.model_dump(mode="json"),
            matched_rule_ids=[rule.rule_id for rule in matched],
            default_applied=not matched,
            observed=observed,
            ruleset_hash=digest,
        )
        artifact = (
            await asyncio.to_thread(
                self._escrow.put,
                applied,
                media_type="application/json",
                decision_id=request.decision_id,
                gate_id=RULES_GATE_ID,
            )
            if self._escrow is not None
            else None
        )
        log.info(
            "rules_gate.evaluated",
            matched_rule_ids=snapshot.matched_rule_ids,
            permitted_actions=[a.value for a in permitted],
            **log_ctx,
        )
        return GateOutput(
            gate_id=RULES_GATE_ID,
            deterministic=True,
            verdict=verdict,
            evidence=_evidence(
                ruleset, matched, content_hash=digest, artifact_hash=artifact
            ),
            evidence_snapshot=snapshot.model_dump(mode="json"),
        )

    async def verify(
        self, output: GateOutput, *, live: bool
    ) -> tuple[EvidenceStatus, ...]:
        """Say how each evidence reference of an output this gate returned stands.

        A reference must carry the digest of the rule set the output's
        snapshot recorded, else it is ``not_verifiable`` (so is every
        reference of a snapshot without its rule set). Without ``live``, the
        rule set escrowed for it is read back: ``intact`` when it is that
        rule set, ``not_verifiable`` when nothing was escrowed or the escrow
        no longer holds it. With ``live``, the rule set's file is read again
        and canonicalized: ``intact`` when it holds the same rules,
        ``source_changed`` when it holds others, ``source_unavailable`` when
        it is gone, unreadable, or not a rule set (or the gate does not know
        it).

        Args:
            output: An output this gate returned, as the bundle stores it.
            live: Whether to read the rule set's file as it is now.

        Returns:
            One status per reference in ``output.evidence``, in order.
        """
        return await asyncio.to_thread(self._verify, output, live=live)

    def _verify(self, output: GateOutput, *, live: bool) -> tuple[EvidenceStatus, ...]:
        refs = output.evidence
        snapshot = _stored_snapshot(output)
        recorded = snapshot.ruleset_hash if snapshot is not None else None
        statuses: list[EvidenceStatus] = []
        now: EvidenceStatus | None = None
        for ref in refs:
            if snapshot is None or recorded is None or ref.content_hash != recorded:
                statuses.append("not_verifiable")
            elif live:
                if now is None:
                    now = self._status_now(snapshot.ruleset_id, recorded)
                statuses.append(now)
            else:
                statuses.append(self._stored_status(ref, recorded))
        return tuple(statuses)

    def _stored_status(self, ref: EvidenceRef, recorded: str) -> EvidenceStatus:
        """The reference checked against the rule set escrowed for it."""
        if ref.artifact_hash is None or self._escrow is None:
            return "not_verifiable"
        content = self._escrow.get(ref.artifact_hash)
        if content is None:
            return "not_verifiable"
        return (
            "intact"
            if hashlib.sha256(content).hexdigest() == recorded
            else "source_changed"
        )

    def _status_now(self, ruleset_id: str, recorded: str) -> EvidenceStatus:
        """The rule set's file, read again, against the rules recorded."""
        path = self._origins.get(ruleset_id)
        if path is None:
            return "source_unavailable"
        try:
            now = hashlib.sha256(canonical_ruleset(read_ruleset(path))).hexdigest()
        except Exception as err:  # unreadable, not YAML, not a rule set, ...
            log.warning(
                "rules_gate.verify_source_unavailable",
                component="rules_gate",
                ruleset_id=ruleset_id,
                path=str(path),
                error=f"{type(err).__name__}: {err}",
            )
            return "source_unavailable"
        return "intact" if now == recorded else "source_changed"

    def _failed(
        self,
        failure: RulesGateFailure,
        detail: str,
        *,
        ruleset_id: str,
        ruleset: RuleSet | None,
        log_ctx: dict[str, str],
        observed: dict[str, Scalar] | None = None,
    ) -> GateOutput:
        """Fail closed: no verdict, with the reason recorded for review."""
        log.warning("rules_gate.no_verdict", failure=failure, detail=detail, **log_ctx)
        snapshot = RulesGateSnapshot(
            ruleset_id=ruleset_id,
            ruleset=ruleset.model_dump(mode="json") if ruleset is not None else None,
            observed=observed or {},
        )
        return GateOutput(
            gate_id=RULES_GATE_ID,
            deterministic=True,
            verdict=None,
            evidence_snapshot=snapshot.model_dump(mode="json"),
            extra={"failure": failure, "detail": detail},
        )


def _evidence(
    ruleset: RuleSet,
    matched: list[Rule],
    *,
    content_hash: str,
    artifact_hash: str | None,
) -> tuple[EvidenceRef, ...]:
    """Cite the matching rules, or the rule set's default when none matched.

    Every reference carries the digest of the whole rule set applied, and of
    its escrowed copy.
    """
    locators = [rule.rule_id for rule in matched] or ["default_action"]
    return tuple(
        EvidenceRef(
            source_id=ruleset.ref,
            locator=locator,
            content_hash=content_hash,
            artifact_hash=artifact_hash,
        )
        for locator in locators
    )


def _stored_snapshot(output: GateOutput) -> RulesGateSnapshot | None:
    """The output's snapshot, or ``None`` when it is not one this gate wrote."""
    raw = output.evidence_snapshot
    if not isinstance(raw, dict):
        return None
    try:
        return RulesGateSnapshot.model_validate_json(json.dumps(raw))
    except ValidationError:
        return None


def _observed_values(ruleset: RuleSet, subject: PredicateSubject) -> dict[str, Scalar]:
    """The subject's value for every field the rule set references."""
    fields = sorted(
        frozenset().union(*(referenced_fields(rule.when) for rule in ruleset.rules))
    )
    observed: dict[str, Scalar] = {}
    for field in fields:
        try:
            observed[field] = resolve_field(field, subject)
        except PredicateError:
            continue
    return observed
