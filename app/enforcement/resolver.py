"""Deterministic enforcement resolver — gate outputs and a gate set into one action.

Enforcement has two levels (DR-28, DR-29).

**Gate-level tiers** apply to one gate's ``GateOutput`` (``resolve_gate_output``),
for every gate that ran or failed, mandatory and advisory alike. They run in
order and the first that fires decides that gate's enforced action:

    1. Schema failure (no verdict, or the gate failed closed:
       an error, a timeout, a malformed output)          → HOLD
    2. Adversarial probe (skipped for a deterministic gate) → BLOCK
    4. Low confidence + high-risk action
       (skipped for a deterministic gate)                → HOLD
    5. Jurisdiction conflict (runs only when the gate's
       evidence carries jurisdictions)                   → HOLD
    6. Default — the most conservative permitted action

**Decision-level rules** apply once per decision (``resolve_decision``):

    3. Novel entity (gated decisions only). When the registration's
       novel-entity feature is truthy, the advisory gates' verdicts are held
       for review (DR-6): the advisory side becomes HOLD, unless an advisory
       gate's own Tier 2 fired (BLOCK) — gate Tiers 1 and 2 outrank Tier 3,
       as they always have. Tier 3 never touches a mandatory gate's action
       (a deployment or registration constraint is not the reasoner's
       uncertainty to waive) and never lowers the reasoner's proposal.
       Recorded decisions skip it: their registered, replayable bypass rule
       is the reasoner's justification, and a rule that should exclude novel
       entities says so in its predicate.
    B. Bypass rules (recorded decisions only). The cited rule was checked
       against the submission's evidence; when it does not hold (or cannot
       be evaluated) the decision fails closed with a HOLD floor. Every other
       rule the reasoner registers was checked too: when one that holds
       justifies a stricter action than the cited rule, the decision is
       floored at that action (the reasoner cannot cite the lenient one of
       two rules its own evidence satisfies).

**The combinator** (``core.gate.combinator.most_restrictive``) then takes the
most restrictive of the proposal, every mandatory gate's enforced action,
the advisory side, and any bypass floor. Every input is a set member or a
maximum, so gate order and parallelism cannot change the result, and adding
a gate can only keep or raise it. BLOCK absorbs: once a mandatory gate's
enforced action is BLOCK the final action is BLOCK whatever the advisory
gates would say, which is why the framework skips them then (and Tier 3 is
not applied). For a single
advisory gate with no proposal and no mandatory gate this reproduces the
single-gate resolver of DR-28 exactly (the action, the credited rule, and the
log), and for a record with no mandatory gate it yields the recorded action.

Every check is logged: each gate's tier lines in its own enforcement, the
decision-level lines and a final combinator line in the decision's, so the
full trail is available for audit without re-running enforcement.

The resolver is reasoner- and gate-agnostic. Tier 3 reads the reasoner's
``novel_entity_feature`` and Tier 5 its ``jurisdiction_conflict_groups`` from
the ``ReasonerRegistration`` passed by the caller; Tiers 2, 4, and 5 read
only the universal ``GateOutput`` fields (``deterministic``, ``verdict``,
``evidence``).

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
from core.gate import most_restrictive, severity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.bundle import DecisionBundle, GateRecord
    from core.gate import GateOutput, GateVerdict
    from core.observation import ReasonerRegistration
    from core.submission import BypassCheck, Submission

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

LOW_CONFIDENCE_THRESHOLD: float = 0.60
ADVERSARIAL_CONTROL: str = "adversarial_probe_detected"
JURISDICTION_CONFLICT_CTL: str = "jurisdiction_conflict"

TIER1_SCHEMA_FAILURE = "tier1_schema_failure"
TIER2_ADVERSARIAL_PROBE = "tier2_adversarial_probe"
TIER3_NOVEL_ENTITY = "tier3_novel_entity"
TIER4_LOW_CONFIDENCE = "tier4_low_confidence"
TIER5_JURISDICTION_CONFLICT = "tier5_jurisdiction_conflict"
BYPASS_RULE_CHECK = "bypass_rule"
BYPASS_RULE_NOT_SATISFIED = "bypass_rule_not_satisfied"
BYPASS_RULE_SUPERSEDED = "bypass_rule_superseded"

_NOVEL_ENTITY_REASON = (
    "Insufficient entity history for confident decision — HOLD for review"
)


# ---------------------------------------------------------------------------
# Gate level
# ---------------------------------------------------------------------------


def resolve_gate_output(
    gate_output: GateOutput | None,
    *,
    registration: ReasonerRegistration,
    decision_id: str,
    failure: str | None = None,
) -> EnforcementDecision:
    """Apply the gate-level tiers (1, 2, 4, 5, 6) to one gate's output.

    Tiers run in order and the first that fires decides the gate's enforced
    action. Tiers 2 and 4 are skipped for a deterministic gate; Tier 5 runs
    only when the gate's ``evidence`` carries jurisdictions.

    Args:
        gate_output: The gate's ``GateOutput``. ``None`` (the gate failed
            closed) and ``verdict is None`` both fire Tier 1.
        registration: The submitting reasoner's registration (Tier 5 reads
            its ``jurisdiction_conflict_groups``).
        decision_id: The decision being enforced, for log correlation.
        failure: Why the gate produced no output (its failure reason), named
            in the Tier 1 line; ``None`` when it returned one.

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

    # --- Tier 1: Schema failure (or no output at all: the gate failed closed) ---
    if gate_output is None or gate_output.verdict is None:
        reason = (
            f"Gate failed ({failure}) — routed to HOLD for review"
            if failure
            else "Gate output failed schema validation — routed to HOLD for review"
        )
        return _fire(TIER1_SCHEMA_FAILURE, DecisionAction.HOLD, reason)
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
# Decision level
# ---------------------------------------------------------------------------


def resolve_decision(
    *,
    submission: Submission,
    records: Sequence[GateRecord],
    registration: ReasonerRegistration,
    bypass_check: BypassCheck | None,
    decision_id: str,
    competing_checks: Sequence[BypassCheck] = (),
) -> EnforcementDecision:
    """Apply the decision-level rules and the combinator to one decision.

    Args:
        submission: The validated submission (its ``mode``,
            ``proposed_action``, and evidence).
        records: One record per gate in the decision's gate set. Records
            whose gate ran or failed carry their gate-level enforcement;
            skipped ones contribute nothing.
        registration: The submitting reasoner's registration (Tier 3 reads
            its ``novel_entity_feature``).
        bypass_check: The cited rule's evaluation, for a record; ``None``
            for a gated decision.
        decision_id: The decision being enforced, for log correlation.
        competing_checks: For a record, the evaluations of every other
            registered bypass rule.

    Returns:
        The decision's action, the decision-level rule that fired (``None``
        when none did, or when an advisory gate's own Tier 1 or 2 outranked
        Tier 3), and the decision-level log ending with the combinator line.

    Raises:
        ValueError: A record arrives without its bypass check, or nothing at
            all contributes an action (no proposal and no gate ran or
            failed — intake validation rules both out).
    """
    log_ctx = {
        "component": "enforcement",
        "decision_id": decision_id,
        "event_id": submission.event_id,
        "mode": submission.mode,
    }
    override_log: list[str] = []
    rule: str | None = None
    proposed = submission.proposed_action
    enforced = [r for r in records if r.enforcement is not None]
    mandatory = [r for r in enforced if r.mandatory]
    advisory = [r for r in enforced if not r.mandatory]

    # (label, action) pairs that enter the combinator, for the log.
    terms: list[tuple[str, DecisionAction]] = [
        (f"mandatory:{r.gate_id}", _action(r)) for r in mandatory
    ]

    if submission.mode == "record":
        if bypass_check is None:
            msg = "a recorded decision is enforced with its bypass check"
            raise ValueError(msg)
        rule = _bypass_rules(bypass_check, competing_checks, override_log, terms)
        override_log.append(
            f"{TIER3_NOVEL_ENTITY}: skipped — recorded decision (the bypass rule "
            "is its justification)"
        )
    elif any(_action(r) == DecisionAction.BLOCK for r in mandatory):
        # BLOCK absorbs: nothing on the advisory side can change the result.
        override_log.append(
            f"{TIER3_NOVEL_ENTITY}: not applied — a mandatory gate already blocks "
            "the decision"
        )
        terms += [("advisory", _action(r)) for r in advisory]
    else:
        tier3_rule, tier3_line, advisory_action = _advisory_side(
            submission, advisory, registration
        )
        rule = tier3_rule
        override_log.append(tier3_line)
        if advisory_action is not None:
            terms.append(("advisory", advisory_action))

    final = most_restrictive(proposed, (action for _, action in terms))
    rendered = [
        *([f"proposed={proposed.value}"] if proposed is not None else []),
        *(f"{label}={action.value}" for label, action in terms),
    ]
    override_log.append(
        f"combinator: most_restrictive({', '.join(rendered)}) → {final.value}"
    )

    log.info(
        "enforcement.resolved",
        decision_action=final.value,
        decision_level_rule=rule,
        gate_actions={r.gate_id: _action(r).value for r in enforced},
        **log_ctx,
    )
    return EnforcementDecision(
        decision_action=final,
        enforcement_rule_applied=rule,
        override_log=override_log,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bypass_rules(
    cited: BypassCheck,
    competing: Sequence[BypassCheck],
    override_log: list[str],
    terms: list[tuple[str, DecisionAction]],
) -> str | None:
    """Apply the record-mode bypass rules; return the rule that fired, if any.

    Appends the log lines and any floor terms. When both floors apply, the
    stricter one is credited (the not-satisfied floor on a tie).
    """
    cited_ref = f"{cited.rule.rule_id}@{cited.rule.version}"
    not_satisfied: DecisionAction | None = None
    if cited.holds:
        override_log.append(
            f"{BYPASS_RULE_CHECK}: {cited_ref} holds — observed {_observed(cited)}"
        )
    else:
        not_satisfied = DecisionAction.HOLD
        why = cited.error or f"observed {_observed(cited)}"
        override_log.append(
            f"{BYPASS_RULE_NOT_SATISFIED}: fired — the cited rule {cited_ref} does "
            f"not hold ({why}) — HOLD for review"
        )
        terms.append(("bypass_floor", not_satisfied))

    stricter = [
        check
        for check in competing
        if check.holds and severity(check.rule.action) > severity(cited.rule.action)
    ]
    superseded: DecisionAction | None = None
    if stricter:
        strictest = max(stricter, key=lambda check: severity(check.rule.action))
        superseded = strictest.rule.action
        override_log.append(
            f"{BYPASS_RULE_SUPERSEDED}: fired — registered rule "
            f"{strictest.rule.rule_id}@{strictest.rule.version} also holds and "
            f"justifies {superseded.value}, stricter than the cited "
            f"{cited.rule.action.value}"
        )
        terms.append(("bypass_floor", superseded))
    elif competing:
        override_log.append(
            f"{BYPASS_RULE_SUPERSEDED}: not fired — no other registered rule that "
            "holds justifies a stricter action"
        )

    if superseded is not None and (
        not_satisfied is None or severity(superseded) > severity(not_satisfied)
    ):
        return BYPASS_RULE_SUPERSEDED
    return BYPASS_RULE_NOT_SATISFIED if not_satisfied is not None else None


def credited_rule(bundle: DecisionBundle) -> str | None:
    """The rule a single-rule summary of the decision credits, if any.

    For callers that report one rule per decision (as the single-gate
    bundle's ``enforcement_rule_applied`` did): the decision-level rule when
    its contribution set the final action, otherwise the gate-level rule of
    the first gate, in record order, whose enforced action equals the final
    action. ``None`` when no rule overrode anything (a gate's verdict
    accepted, or the recorded action).

    Args:
        bundle: A version-2 bundle.

    Returns:
        The credited rule name, or ``None``.
    """
    final = bundle.decision_action
    rule = bundle.decision_level_rule
    if rule in (TIER3_NOVEL_ENTITY, BYPASS_RULE_NOT_SATISFIED) and (
        final == DecisionAction.HOLD
    ):
        return rule
    if rule == BYPASS_RULE_SUPERSEDED:
        cited = bundle.bypass_check
        floors = [
            check.rule.action
            for check in bundle.other_bypass_checks
            if check.holds
            and cited is not None
            and severity(check.rule.action) > severity(cited.rule.action)
        ]
        if floors and max(floors, key=severity) == final:
            return rule
    for record in bundle.gates:
        if record.enforcement is not None and record.enforced_action == final:
            return record.enforcement.enforcement_rule_applied
    return None


def _action(record: GateRecord) -> DecisionAction:
    """The record's gate-level enforced action (records here always have one)."""
    assert record.enforcement is not None
    return record.enforcement.decision_action


def _advisory_side(
    submission: Submission,
    advisory: list[GateRecord],
    registration: ReasonerRegistration,
) -> tuple[str | None, str, DecisionAction | None]:
    """Combine the advisory gates of a gated decision, applying Tier 3 once.

    Returns:
        ``(rule, log_line, action)``: ``rule`` is ``TIER3_NOVEL_ENTITY`` when
        Tier 3 decided the advisory side, ``action`` the advisory side's
        contribution to the combinator (``None`` when no advisory gate ran
        and Tier 3 did not fire).
    """
    gate_actions = [_action(r) for r in advisory]
    plain = max(gate_actions, key=severity) if gate_actions else None
    novel_feature = registration.novel_entity_feature
    if novel_feature is None:
        line = f"{TIER3_NOVEL_ENTITY}: not fired — no novel-entity feature configured"
        return None, line, plain
    if not bool(submission.reasoner_context.feature_set.get(novel_feature, False)):
        return None, f"{TIER3_NOVEL_ENTITY}: not fired — history sufficient", plain

    rules = {r.enforcement.enforcement_rule_applied for r in advisory if r.enforcement}
    line = f"{TIER3_NOVEL_ENTITY}: fired — {_NOVEL_ENTITY_REASON}"
    if TIER2_ADVERSARIAL_PROBE in rules:
        # An advisory gate saw an adversarial probe: its BLOCK stands.
        return (
            None,
            f"{line}; {TIER2_ADVERSARIAL_PROBE} outranks it",
            (DecisionAction.BLOCK),
        )
    if advisory and all(
        r.enforcement is not None
        and r.enforcement.enforcement_rule_applied == TIER1_SCHEMA_FAILURE
        for r in advisory
    ):
        # Every advisory gate already holds for its own Tier 1.
        return None, f"{line}; {TIER1_SCHEMA_FAILURE} outranks it", DecisionAction.HOLD
    return TIER3_NOVEL_ENTITY, line, DecisionAction.HOLD


def _observed(check: BypassCheck) -> str:
    """Render the observed values a bypass rule read, deterministically."""
    if not check.observed:
        return "{}"
    return ", ".join(
        f"{field}={value!r}" for field, value in sorted(check.observed.items())
    )


def _most_conservative(actions: list[DecisionAction]) -> DecisionAction:
    """Return the most conservative action from a non-empty list."""
    if not actions:
        # Permissionless gate output — default to most conservative possible.
        return DecisionAction.HOLD
    return max(actions, key=severity)


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
