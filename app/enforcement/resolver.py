"""Deterministic enforcement resolver — translates gate output into a decision action.

Enforcement has two levels (DR-28):

**Gate-level tiers** apply to one gate's ``GateOutput``
(``resolve_gate_output``). They run in order and the first that fires wins:

    1. Schema failure (no verdict)                       → HOLD
    2. Adversarial probe (skipped for a deterministic gate) → BLOCK
    4. Low confidence + high-risk action
       (skipped for a deterministic gate)                → HOLD
    5. Jurisdiction conflict (runs only when the gate's
       evidence carries jurisdictions)                   → HOLD
    6. Default — the most conservative permitted action

**Decision-level tiers** apply once per decision, independent of any gate
(``resolve``):

    3. Novel entity (insufficient history)               → HOLD (route to
       human review, DR-6)

``resolve()`` combines the two in the historical tier priority, so a
single-gate decision resolves exactly as the single-pass resolver did: gate
Tiers 1 and 2 outrank the decision-level Tier 3, and Tier 3 outranks gate
Tiers 4 and 5 and the gate's accepted default — a novel entity is held for
review whatever the gate would otherwise have let through or blocked. How
the decision-level tier composes with several gates (and whether it may
hold a mandatory gate's BLOCK for review) is the gate-set combinator's
decision (DR-29). Every tier check is logged in ``override_log`` (gate-level
entries first, then the decision-level entry), so the full decision trail is
available for audit without re-running enforcement.

Fast-path observations (FAST_PATH_ALLOW / FAST_PATH_BLOCK) return immediately
with no tier evaluation — the reasoner was confident enough to bypass the
gate.

The resolver is reasoner- and gate-agnostic. Tier 3 reads the reasoner's
``novel_entity_feature`` and Tier 5 its ``jurisdiction_conflict_groups`` from
the ``ReasonerRegistration`` passed by the caller; Tiers 2, 4, and 5 read
only the universal ``GateOutput`` fields (``deterministic``, ``verdict``,
``evidence``). A registration that leaves a profile field unset disables the
corresponding structural check.

The enforcement layer produces the bundle's ``decision_action``. For
non-terminal actions (CHALLENGE, HOLD) the realized outcome is determined
later via the resolution attempt log (see ``core.resolution``); enforcement is
not concerned with that downstream lifecycle.

Tier constants (tune without changing resolver logic):
    LOW_CONFIDENCE_THRESHOLD  — below this, gate output confidence is considered
                                 insufficient for high-stakes actions.
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
    from core.gate import GateOutput, GateVerdict
    from core.observation import Observation, ReasonerRegistration

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

LOW_CONFIDENCE_THRESHOLD: float = 0.60
ADVERSARIAL_CONTROL: str = "adversarial_probe_detected"
JURISDICTION_CONFLICT_CTL: str = "jurisdiction_conflict"

_SEVERITY_ORDER = [
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
]

TIER1_SCHEMA_FAILURE = "tier1_schema_failure"
TIER2_ADVERSARIAL_PROBE = "tier2_adversarial_probe"
TIER3_NOVEL_ENTITY = "tier3_novel_entity"
TIER4_LOW_CONFIDENCE = "tier4_low_confidence"
TIER5_JURISDICTION_CONFLICT = "tier5_jurisdiction_conflict"

#: Gate-level rules that outrank the decision-level Tier 3 (they sit before
#: it in the historical tier order).
_OUTRANKS_TIER3 = frozenset({TIER1_SCHEMA_FAILURE, TIER2_ADVERSARIAL_PROBE})

_NOVEL_ENTITY_REASON = (
    "Insufficient entity history for confident decision — HOLD for review"
)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def resolve(
    obs: Observation,
    gate_output: GateOutput | None,
    *,
    decision_id: str,
    registration: ReasonerRegistration,
) -> EnforcementDecision:
    """Resolve a decision action from the gate's output and the observation.

    On the gate route, applies the gate-level tiers to ``gate_output`` and
    the decision-level tier once, and combines them in the historical tier
    priority (gate Tiers 1-2, then Tier 3, then gate Tiers 4-6). All tier
    checks are recorded in the returned ``override_log``.

    Args:
        obs: The assembled Observation from the reasoner.
        gate_output: The gate's universal ``GateOutput``, or ``None`` when no
            gate ran. On the gate route ``None`` is treated like a missing
            verdict (Tier 1 holds); on the fast path it is ignored.
        decision_id: The ``decision_id`` of the bundle being assembled.
            Carried in the structured log context.
        registration: The submitting reasoner's registration. Supplies the
            enforcement profile: ``novel_entity_feature`` (Tier 3) and
            ``jurisdiction_conflict_groups`` (Tier 5). Required — passing
            it explicitly keeps "this reasoner has no novel-entity signal"
            a deliberate declaration rather than a silent omission.

    Returns:
        An immutable ``EnforcementDecision`` with the decision action, the
        override rule credited (if any), and the full override log.
    """
    log_ctx = {
        "component": "enforcement",
        "decision_id": decision_id,
        "event_id": obs.event_id,
        "route": obs.route.value,
    }

    # Fast-path: reasoner was high-confidence — no tier evaluation.
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

    # Gate route: gate-level tiers, then the decision-level tier, combined
    # in the historical priority.
    gate_level = resolve_gate_output(
        gate_output, registration=registration, decision_id=decision_id
    )
    outranked_by = (
        gate_level.enforcement_rule_applied
        if gate_level.enforcement_rule_applied in _OUTRANKS_TIER3
        else None
    )
    novel_entity, tier3_line = _novel_entity_tier(
        obs, registration, outranked_by=outranked_by
    )
    if novel_entity and outranked_by is None:
        final_action, rule = DecisionAction.HOLD, TIER3_NOVEL_ENTITY
    else:
        final_action = gate_level.decision_action
        rule = gate_level.enforcement_rule_applied

    log.info(
        "enforcement.resolved",
        decision_action=final_action.value,
        enforcement_rule_applied=rule,
        gate_action=gate_level.decision_action.value,
        novel_entity=novel_entity,
        **log_ctx,
    )
    return EnforcementDecision(
        decision_action=final_action,
        enforcement_rule_applied=rule,
        override_log=[*gate_level.override_log, tier3_line],
    )


def resolve_gate_output(
    gate_output: GateOutput | None,
    *,
    registration: ReasonerRegistration,
    decision_id: str,
) -> EnforcementDecision:
    """Apply the gate-level tiers (1, 2, 4, 5, 6) to one gate's output.

    Tiers run in order and the first that fires decides the gate's enforced
    action. Tiers 2 and 4 are skipped for a deterministic gate; Tier 5 runs
    only when the gate's ``evidence`` carries jurisdictions.

    Args:
        gate_output: The gate's ``GateOutput``. ``None`` (no output) and
            ``verdict is None`` both fire Tier 1.
        registration: The submitting reasoner's registration (Tier 5 reads
            its ``jurisdiction_conflict_groups``).
        decision_id: The decision being enforced, for log correlation.

    Returns:
        The gate's enforced action, the rule that fired (``None`` when the
        default accepted the gate's verdict), and the gate-level log.
    """
    log_ctx = {
        "component": "enforcement",
        "decision_id": decision_id,
        "gate_id": gate_output.gate_id if gate_output is not None else None,
    }
    override_log: list[str] = []

    def _fire(rule: str, action: DecisionAction, reason: str) -> EnforcementDecision:
        override_log.append(f"{rule}: fired — {reason}")
        log.info("enforcement.gate_tier_fired", rule=rule, action=action, **log_ctx)
        return EnforcementDecision(
            decision_action=action,
            enforcement_rule_applied=rule,
            override_log=override_log,
        )

    # --- Tier 1: Schema failure ---
    if gate_output is None or gate_output.verdict is None:
        return _fire(
            TIER1_SCHEMA_FAILURE,
            DecisionAction.HOLD,
            "Gate output failed schema validation — routed to HOLD for review",
        )
    override_log.append(f"{TIER1_SCHEMA_FAILURE}: not fired — gate output present")
    verdict = gate_output.verdict

    # --- Tier 2: Adversarial probe (meaningless for a deterministic gate) ---
    if gate_output.deterministic:
        override_log.append(f"{TIER2_ADVERSARIAL_PROBE}: skipped — deterministic gate")
    elif ADVERSARIAL_CONTROL in verdict.required_controls:
        return _fire(
            TIER2_ADVERSARIAL_PROBE,
            DecisionAction.BLOCK,
            f"Gate flagged adversarial probe via {ADVERSARIAL_CONTROL!r}",
        )
    else:
        override_log.append(f"{TIER2_ADVERSARIAL_PROBE}: not fired")

    # --- Tier 4: Low confidence + high-risk action ---
    conservative = _most_conservative(verdict.permitted_actions)
    if gate_output.deterministic:
        override_log.append(f"{TIER4_LOW_CONFIDENCE}: skipped — deterministic gate")
    elif verdict.confidence < LOW_CONFIDENCE_THRESHOLD and conservative in (
        DecisionAction.HOLD,
        DecisionAction.BLOCK,
    ):
        return _fire(
            TIER4_LOW_CONFIDENCE,
            DecisionAction.HOLD,
            (
                f"Gate confidence {verdict.confidence:.2f} below threshold "
                f"{LOW_CONFIDENCE_THRESHOLD} with high-risk action {conservative.value}"
            ),
        )
    else:
        override_log.append(
            f"{TIER4_LOW_CONFIDENCE}: not fired — confidence={verdict.confidence:.2f}"
        )

    # --- Tier 5: Jurisdiction conflict (only when evidence carries any) ---
    jurisdictions = gate_output.evidence_jurisdictions()
    if not jurisdictions:
        override_log.append(
            f"{TIER5_JURISDICTION_CONFLICT}: skipped — gate evidence carries "
            "no jurisdictions"
        )
    elif _has_jurisdiction_conflict(
        verdict, jurisdictions, registration.jurisdiction_conflict_groups
    ):
        return _fire(
            TIER5_JURISDICTION_CONFLICT,
            DecisionAction.HOLD,
            "Conflicting policy requirements across jurisdictions — HOLD for review",
        )
    else:
        override_log.append(f"{TIER5_JURISDICTION_CONFLICT}: not fired")

    # --- Tier 6: Default — accept gate output ---
    override_log.append(
        f"tier6_default: accepted gate output — action={conservative.value}"
    )
    return EnforcementDecision(
        decision_action=conservative,
        enforcement_rule_applied=None,
        override_log=override_log,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _novel_entity_tier(
    obs: Observation,
    registration: ReasonerRegistration,
    *,
    outranked_by: str | None,
) -> tuple[bool, str]:
    """Evaluate decision-level Tier 3 once; return (fired, override-log line).

    ``outranked_by`` names the gate-level rule (Tier 1 or 2) that decides the
    action even when Tier 3 fires; the log line records it.
    """
    novel_feature = registration.novel_entity_feature
    if novel_feature is None:
        return (
            False,
            f"{TIER3_NOVEL_ENTITY}: not fired — no novel-entity feature configured",
        )
    feature_set = obs.reasoner_context.feature_set if obs.reasoner_context else {}
    if not bool(feature_set.get(novel_feature, False)):
        return False, f"{TIER3_NOVEL_ENTITY}: not fired — history sufficient"
    line = f"{TIER3_NOVEL_ENTITY}: fired — {_NOVEL_ENTITY_REASON}"
    if outranked_by is not None:
        line += f"; {outranked_by} outranks it"
    return True, line


def _most_conservative(actions: list[DecisionAction]) -> DecisionAction:
    """Return the most conservative action from a non-empty list."""
    if not actions:
        # Permissionless gate output — default to most conservative possible.
        return DecisionAction.HOLD
    return max(actions, key=_SEVERITY_ORDER.index)


def _has_jurisdiction_conflict(
    verdict: GateVerdict,
    jurisdictions: frozenset[str],
    conflict_groups: tuple[frozenset[str], ...],
) -> bool:
    """Return True if a jurisdiction conflict is signaled.

    Two detection paths: the gate flags it explicitly (via the
    ``JURISDICTION_CONFLICT_CTL`` control or a conflict-worded escalation),
    or the jurisdictions its evidence carries structurally span at least two
    distinct ``conflict_groups``. An empty ``conflict_groups`` disables the
    structural path.
    """
    # Gate explicitly flagged it.
    if JURISDICTION_CONFLICT_CTL in verdict.required_controls:
        return True
    if (
        verdict.escalate_to_human
        and verdict.escalation_reason
        and (
            "jurisdiction" in verdict.escalation_reason.lower()
            or "conflict" in verdict.escalation_reason.lower()
        )
    ):
        return True
    # Structural detection: the evidence spans two or more distinct conflict
    # groups declared on the reasoner's registration.
    groups_touched = {g for g in conflict_groups if jurisdictions & g}
    return len(groups_touched) >= 2
