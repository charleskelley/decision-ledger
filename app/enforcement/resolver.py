"""Deterministic enforcement resolver — translates gate output into a final action.

resolve() is the only public entry point. It evaluates six priority tiers in
order and returns on the first trigger that fires. Every tier check is logged
in override_log regardless of whether it fired, so the full decision trail is
available for audit without re-running enforcement.

Fast-path observations (FAST_PATH_ALLOW / FAST_PATH_BLOCK) return immediately
with no tier evaluation — the domain reasoner was confident enough to bypass
the gate.

Tier constants (tune without changing resolver logic):
    LOW_CONFIDENCE_THRESHOLD  — below this, gate output confidence is considered
                                 insufficient for high-stakes actions.
    NOVEL_ENTITY_FEATURE      — feature_set key that signals insufficient history.
    ADVERSARIAL_CONTROL       — required_controls sentinel the gate emits when it
                                 detects a prompt injection or adversarial probe.
    JURISDICTION_CONFLICT_CTL — required_controls sentinel for detected conflicts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from core.actions import DecisionAction
from core.bundle import EnforcementDecision, ReviewPacket
from core.gate import GateRouting

if TYPE_CHECKING:
    from core.bundle import PolicySnippet
    from core.gate import PolicyGateOutput
    from core.observation import Observation, Signal

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

LOW_CONFIDENCE_THRESHOLD: float = 0.60
NOVEL_ENTITY_FEATURE: str = "sparse_history"
ADVERSARIAL_CONTROL: str = "adversarial_probe_detected"
JURISDICTION_CONFLICT_CTL: str = "jurisdiction_conflict"

_SEVERITY_ORDER = [
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
]

_US_JURISDICTIONS = frozenset({"US_FEDERAL", "US_STATE"})
_EU_JURISDICTIONS = frozenset({"EU_GDPR"})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve(
    obs: Observation,
    gate_output: PolicyGateOutput | None,
    *,
    snippets: list[PolicySnippet],
    decision_id: str,
) -> EnforcementDecision:
    """Resolve a final action from the policy gate output and observation context.

    Evaluates six priority tiers in order and returns on the first trigger.
    All tier checks are recorded in the returned ``override_log`` for full
    audit traceability.

    Args:
        obs: The assembled Observation from the domain reasoner.
        gate_output: Validated policy gate output, or ``None`` if the LLM
            response failed schema validation or the gate was not invoked.
        snippets: Policy snippets returned by the retriever for this
            observation. Used for jurisdiction-conflict detection (Tier 5).
        decision_id: The ``decision_id`` of the bundle being assembled.
            Used to populate ``ReviewPacket.decision_id`` on HOLD decisions.

    Returns:
        An immutable ``EnforcementDecision`` with the final action, the
        override rule that fired (if any), the full override log, and a
        ``ReviewPacket`` for HOLD decisions.
    """
    log_ctx = {
        "component": "enforcement",
        "decision_id": decision_id,
        "event_id": obs.event_id,
        "routing": obs.routing.value,
    }

    # Fast-path: domain reasoner was high-confidence — no tier evaluation.
    if obs.routing == GateRouting.FAST_PATH_ALLOW:
        log.debug("enforcement.fast_path_allow", **log_ctx)
        return EnforcementDecision(
            final_action=DecisionAction.ALLOW,
            enforcement_rule_applied=None,
            override_log=["fast_path_allow: FAST_PATH_ALLOW — policy gate skipped"],
            review_packet=None,
        )

    if obs.routing == GateRouting.FAST_PATH_BLOCK:
        log.debug("enforcement.fast_path_block", **log_ctx)
        return EnforcementDecision(
            final_action=DecisionAction.BLOCK,
            enforcement_rule_applied=None,
            override_log=["fast_path_block: FAST_PATH_BLOCK — policy gate skipped"],
            review_packet=None,
        )

    # Gate-path: evaluate tiers in priority order.
    override_log: list[str] = []
    risk_score = _extract_risk_score(obs)
    top_signals = _extract_signals(obs)

    def _make_hold(rule: str, reason: str, *, priority: int = 0) -> EnforcementDecision:
        override_log.append(f"{rule}: fired — {reason}")
        log.info("enforcement.hold", rule=rule, **log_ctx)
        return EnforcementDecision(
            final_action=DecisionAction.HOLD,
            enforcement_rule_applied=rule,
            override_log=override_log,
            review_packet=ReviewPacket(
                decision_id=decision_id,
                entity_id=obs.entity_id,
                created_at=datetime.now(UTC),
                hold_reason=reason,
                enforcement_rule=rule,
                risk_score=risk_score,
                top_signals=top_signals,
                priority=priority,
            ),
        )

    def _make_block(rule: str, reason: str) -> EnforcementDecision:
        override_log.append(f"{rule}: fired — {reason}")
        log.info("enforcement.block_override", rule=rule, **log_ctx)
        return EnforcementDecision(
            final_action=DecisionAction.BLOCK,
            enforcement_rule_applied=rule,
            override_log=override_log,
            review_packet=None,
        )

    # --- Tier 1: Schema failure ---
    if gate_output is None:
        return _make_hold(
            "tier1_schema_failure",
            "Policy gate output failed schema validation — routed to HOLD for review",
            priority=2,
        )
    override_log.append("tier1_schema_failure: not fired — gate output present")

    # --- Tier 2: Adversarial probe ---
    if ADVERSARIAL_CONTROL in gate_output.required_controls:
        return _make_block(
            "tier2_adversarial_probe",
            f"Gate flagged adversarial probe via {ADVERSARIAL_CONTROL!r}",
        )
    override_log.append("tier2_adversarial_probe: not fired")

    # --- Tier 3: Novel entity (sparse history) ---
    feature_set = obs.reasoner_context.feature_set if obs.reasoner_context else {}
    is_sparse = bool(feature_set.get(NOVEL_ENTITY_FEATURE, False))
    if is_sparse:
        return _make_hold(
            "tier3_novel_entity",
            "Insufficient account history for confident decision — HOLD for review",
            priority=1,
        )
    override_log.append("tier3_novel_entity: not fired — history sufficient")

    # --- Tier 4: Low confidence + high-risk action ---
    conservative = _most_conservative(gate_output.permitted_actions)
    if gate_output.confidence < LOW_CONFIDENCE_THRESHOLD and conservative in (
        DecisionAction.HOLD,
        DecisionAction.BLOCK,
    ):
        return _make_hold(
            "tier4_low_confidence",
            (
                f"Gate confidence {gate_output.confidence:.2f} below threshold "
                f"{LOW_CONFIDENCE_THRESHOLD} with high-risk action {conservative.value}"
            ),
            priority=1,
        )
    override_log.append(
        f"tier4_low_confidence: not fired — confidence={gate_output.confidence:.2f}"
    )

    # --- Tier 5: Jurisdiction conflict ---
    if _has_jurisdiction_conflict(gate_output, snippets):
        return _make_hold(
            "tier5_jurisdiction_conflict",
            "Conflicting policy requirements across jurisdictions — HOLD for review",
            priority=2,
        )
    override_log.append("tier5_jurisdiction_conflict: not fired")

    # --- Tier 6: Default — accept gate output ---
    final = _most_conservative(gate_output.permitted_actions)
    override_log.append(f"tier6_default: accepted gate output — action={final.value}")
    log.info(
        "enforcement.resolved",
        final_action=final.value,
        gate_confidence=gate_output.confidence,
        **log_ctx,
    )
    return EnforcementDecision(
        final_action=final,
        enforcement_rule_applied=None,
        override_log=override_log,
        review_packet=(
            ReviewPacket(
                decision_id=decision_id,
                entity_id=obs.entity_id,
                created_at=datetime.now(UTC),
                hold_reason="Gate output accepted — HOLD per gate recommendation",
                enforcement_rule=None,
                risk_score=risk_score,
                top_signals=top_signals,
                priority=0,
            )
            if final == DecisionAction.HOLD
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _most_conservative(actions: list[DecisionAction]) -> DecisionAction:
    """Return the most conservative action from a non-empty list."""
    if not actions:
        # Permissionless gate output — default to most conservative possible.
        return DecisionAction.HOLD
    return max(actions, key=_SEVERITY_ORDER.index)


def _has_jurisdiction_conflict(
    gate_output: PolicyGateOutput,
    snippets: list[PolicySnippet],
) -> bool:
    """Return True if a jurisdiction conflict is signalled."""
    # Gate explicitly flagged it.
    if JURISDICTION_CONFLICT_CTL in gate_output.required_controls:
        return True
    if (
        gate_output.escalate_to_human
        and gate_output.escalation_reason
        and (
            "jurisdiction" in gate_output.escalation_reason.lower()
            or "conflict" in gate_output.escalation_reason.lower()
        )
    ):
        return True
    # Structural detection: both US and EU policy chunks present in top results.
    jurisdictions = {s.jurisdiction for s in snippets}
    return bool(jurisdictions & _US_JURISDICTIONS) and bool(
        jurisdictions & _EU_JURISDICTIONS
    )


def _extract_risk_score(obs: Observation) -> float:
    """Extract the numeric risk score from reasoner_context, defaulting to 0.0."""
    rc = obs.reasoner_context
    if rc is None:
        return 0.0
    try:
        return float(rc.label_value)
    except (TypeError, ValueError):
        return 0.0


def _extract_signals(obs: Observation) -> list[Signal]:
    """Extract the top SHAP signals from reasoner_context attribution."""
    rc = obs.reasoner_context
    if rc is None or rc.attribution is None:
        return []
    return list(rc.attribution.observation_signals or [])
