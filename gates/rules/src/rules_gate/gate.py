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
  carry no jurisdictions, so the jurisdiction-conflict tier is skipped.
- ``evidence_snapshot`` — a ``RulesGateSnapshot``: the full rule set as
  evaluated, the matching rule ids, and the observed values of every field
  the rules reference, so the decision is auditable without the rule files.
- ``extra`` — ``{"failure": ..., "detail": ...}`` when there is no verdict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from core.exceptions import PredicateError
from core.gate import EvidenceRef, GateOutput, GateVerdict, evaluate_predicate
from core.gate.predicate import Scalar, referenced_fields, resolve_field
from rules_gate.ruleset import RulesGateConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    """

    model_config = ConfigDict(strict=True, frozen=True)

    ruleset_id: str
    ruleset: JsonValue | None
    matched_rule_ids: list[str] = Field(default_factory=list)
    default_applied: bool = False
    observed: dict[str, Scalar] = Field(default_factory=dict)


class RulesGate:
    """Deterministic gate over registered rule sets.

    Satisfies ``app.gates.Gate``. Holds no connections; ``close()`` is a
    no-op.

    Args:
        rulesets: Registered rule sets keyed by ``ruleset_id``.
    """

    def __init__(self, rulesets: Mapping[str, RuleSet]) -> None:
        """Bind the registered rule sets."""
        self._rulesets = dict(rulesets)

    @property
    def ruleset_ids(self) -> list[str]:
        """The registered rule set ids, sorted."""
        return sorted(self._rulesets)

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
        subject = request.observation.reasoner_context
        # Intake validation guarantees a populated reasoner_context.
        assert subject is not None
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
        snapshot = RulesGateSnapshot(
            ruleset_id=ruleset.ruleset_id,
            ruleset=ruleset.model_dump(mode="json"),
            matched_rule_ids=[rule.rule_id for rule in matched],
            default_applied=not matched,
            observed=observed,
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
            evidence=_evidence(ruleset, matched),
            evidence_snapshot=snapshot.model_dump(mode="json"),
        )

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


def _evidence(ruleset: RuleSet, matched: list[Rule]) -> tuple[EvidenceRef, ...]:
    """Cite the matching rules, or the rule set's default when none matched."""
    if not matched:
        return (EvidenceRef(source_id=ruleset.ref, locator="default_action"),)
    return tuple(
        EvidenceRef(source_id=ruleset.ref, locator=rule.rule_id) for rule in matched
    )


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
