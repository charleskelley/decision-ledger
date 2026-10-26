"""The daily mock's assembler — a computed settlement into a ``Submission``.

The handoff boundary between the mock's domain and the ledger: the row
crosses only as opaque ``raw_event`` JSON, and the computation only as the
``ReasonerContext`` (with every value the computation and the bypass rule
read, so replay needs nothing else). The mode comes from the registration's
bypass rule (``matching_bypass_rule``, DR-30): an account within tolerance
is *recorded* ALLOW, citing ``within-tolerance`` pinned to its version; any
other account is an exception, *gated* with no proposal, and the rules gate
decides it.

Every submission addresses the rules gate, which the registration binds
mandatory, so it runs on recorded submissions too, and names the rule set it
is decided under (``RULESET_ID``) in its gate context: the binding carries
no configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observation import AttributionSummary, GateContext, LabelType, ReasonerContext
from core.submission import BypassCitation, Submission, matching_bypass_rule
from mock_daily_reasoner.compute import COMPUTE_VERSION
from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION, RULESET_ID

if TYPE_CHECKING:
    from core.observation import ReasonerRegistration
    from mock_daily_reasoner.compute import SettlementDrift
    from mock_daily_reasoner.settlements import AccountSettlement

#: The gate the mock's registration binds.
RULES_GATE_ID = "rules"


def reasoner_context(drift: SettlementDrift) -> ReasonerContext:
    """The computation's evidence as the ledger's ``ReasonerContext``."""
    return ReasonerContext(
        reasoner_id=MOCK_DAILY_REGISTRATION.reasoner_id,
        reasoner_name=MOCK_DAILY_REGISTRATION.reasoner_name,
        model_version=COMPUTE_VERSION,
        label_type=LabelType.NUMERICAL,
        label_name="drift_pct",
        label_value=drift.drift_pct,
        feature_set=drift.feature_set,
        attribution=AttributionSummary(narrative=drift.narrative),
    )


def rules_gate_context() -> GateContext:
    """The rules gate's context: the rule set every row is decided under."""
    return GateContext(gate_id=RULES_GATE_ID, gate_config={"ruleset_id": RULESET_ID})


def build_submission(
    settlement: AccountSettlement,
    drift: SettlementDrift,
    *,
    registration: ReasonerRegistration = MOCK_DAILY_REGISTRATION,
) -> Submission:
    """Assemble the mock's ``Submission`` for one computed settlement.

    Args:
        settlement: The account's settlement for the day.
        drift: Its computed drift.
        registration: The registration whose bypass rule picks the mode.

    Returns:
        A ``record`` submission (ALLOW and a version-pinned citation of
        ``within-tolerance``) when the account is within tolerance;
        otherwise a ``gate`` submission with no proposal (an exception).
        Both address the rules gate, name the account as their native
        subject, and carry the row as ``raw_event``.
    """
    context = reasoner_context(drift)
    rule = matching_bypass_rule(registration, context)
    return Submission(
        reasoner_id=registration.reasoner_id,
        event_id=settlement.event_id,
        timestamp=settlement.as_of,
        entity_id=settlement.entity_id,
        entity_type=settlement.entity_type,
        native_subject=settlement.native_subject,
        mode="record" if rule is not None else "gate",
        proposed_action=rule.action if rule is not None else None,
        bypass=(
            BypassCitation(rule_id=rule.rule_id, rule_version=rule.version)
            if rule is not None
            else None
        ),
        reasoner_context=context,
        gate_contexts=(rules_gate_context(),),
        raw_event=settlement.payload(),
    )
