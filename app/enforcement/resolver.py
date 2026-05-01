"""Deterministic enforcement resolver — translates gate output into a decision action.

resolve() is the only public entry point. It evaluates six priority tiers in
order and returns on the first trigger that fires. Every tier check is logged
in override_log regardless of whether it fired, so the full decision trail is
available for audit without re-running enforcement.

Fast-path observations (FAST_PATH_ALLOW / FAST_PATH_BLOCK) return immediately
with no tier evaluation — the domain reasoner was confident enough to bypass
the gate.

The enforcement layer produces the bundle's ``decision_action``. For
non-terminal actions (CHALLENGE, HOLD) the realized outcome is determined
later via the resolution attempt log (see ``core.resolution``); enforcement is
not concerned with that downstream lifecycle.

Tier constants (tune without changing resolver logic):
    LOW_CONFIDENCE_THRESHOLD  — below this, gate output confidence is considered
                                 insufficient for high-stakes actions.
    NOVEL_ENTITY_FEATURE      — feature_set key that signals insufficient history.
    ADVERSARIAL_CONTROL       — required_controls sentinel the gate emits when it
                                 detects a prompt injection or adversarial probe.
    JURISDICTION_CONFLICT_CTL — required_controls sentinel for detected conflicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from core.actions import DecisionAction
from core.enforcement import EnforcementDecision
from core.routes import GateRoute

if TYPE_CHECKING:
    from core.gate import GateVerdict
    from core.observation import Observation
    from core.snippet import RetrievedSnippet

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
    gate_output: GateVerdict | None,
    *,
    snippets: list[RetrievedSnippet],
    decision_id: str,
) -> EnforcementDecision:
    """Resolve a decision action from the gate's typed verdict and observation context.

    Evaluates six priority tiers in order and returns on the first trigger.
    All tier checks are recorded in the returned ``override_log`` for full
    audit traceability.

    Args:
        obs: The assembled Observation from the domain reasoner.
        gate_output: Validated gate verdict (a ``GateVerdict``), or ``None``
            if the gate response failed schema validation or the gate was
            not invoked.
        snippets: Policy snippets returned by the retriever for this
            observation. Used for jurisdiction-conflict detection (Tier 5).
        decision_id: The ``decision_id`` of the bundle being assembled.
            Carried in the structured log context.

    Returns:
        An immutable ``EnforcementDecision`` with the decision action, the
        override rule that fired (if any), and the full override log.
    """
    log_ctx = {
        "component": "enforcement",
        "decision_id": decision_id,
        "event_id": obs.event_id,
        "route": obs.route.value,
    }

    # Fast-path: domain reasoner was high-confidence — no tier evaluation.
    if obs.route == GateRoute.FAST_PATH_ALLOW:
        log.debug("enforcement.fast_path_allow", **log_ctx)
        return EnforcementDecision(
            decision_action=DecisionAction.ALLOW,
            enforcement_rule_applied=None,
            override_log=["fast_path_allow: FAST_PATH_ALLOW — policy gate skipped"],
        )

    if obs.route == GateRoute.FAST_PATH_BLOCK:
        log.debug("enforcement.fast_path_block", **log_ctx)
        return EnforcementDecision(
            decision_action=DecisionAction.BLOCK,
            enforcement_rule_applied=None,
            override_log=["fast_path_block: FAST_PATH_BLOCK — policy gate skipped"],
        )

    # Gate-path: evaluate tiers in priority order.
    override_log: list[str] = []

    def _make_hold(rule: str, reason: str) -> EnforcementDecision:
        override_log.append(f"{rule}: fired — {reason}")
        log.info("enforcement.hold", rule=rule, **log_ctx)
        return EnforcementDecision(
            decision_action=DecisionAction.HOLD,
            enforcement_rule_applied=rule,
            override_log=override_log,
        )

    def _make_block(rule: str, reason: str) -> EnforcementDecision:
        override_log.append(f"{rule}: fired — {reason}")
        log.info("enforcement.block_override", rule=rule, **log_ctx)
        return EnforcementDecision(
            decision_action=DecisionAction.BLOCK,
            enforcement_rule_applied=rule,
            override_log=override_log,
        )

    # --- Tier 1: Schema failure ---
    if gate_output is None:
        return _make_hold(
            "tier1_schema_failure",
            "Policy gate output failed schema validation — routed to HOLD for review",
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
        )
    override_log.append(
        f"tier4_low_confidence: not fired — confidence={gate_output.confidence:.2f}"
    )

    # --- Tier 5: Jurisdiction conflict ---
    if _has_jurisdiction_conflict(gate_output, snippets):
        return _make_hold(
            "tier5_jurisdiction_conflict",
            "Conflicting policy requirements across jurisdictions — HOLD for review",
        )
    override_log.append("tier5_jurisdiction_conflict: not fired")

    # --- Tier 6: Default — accept gate output ---
    action = _most_conservative(gate_output.permitted_actions)
    override_log.append(f"tier6_default: accepted gate output — action={action.value}")
    log.info(
        "enforcement.resolved",
        decision_action=action.value,
        gate_confidence=gate_output.confidence,
        **log_ctx,
    )
    return EnforcementDecision(
        decision_action=action,
        enforcement_rule_applied=None,
        override_log=override_log,
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
    gate_output: GateVerdict,
    snippets: list[RetrievedSnippet],
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
