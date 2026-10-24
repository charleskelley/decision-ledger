"""The batch mock's assembler — a scored row into a ``Submission``.

The handoff boundary between the mock's domain and the ledger: the row
crosses only as opaque ``raw_event`` JSON, and the score only as the
``ReasonerContext`` (with every feature the scorer and the bypass rules read,
so replay needs nothing else). The mode comes from the registration's bypass
rules (``matching_bypass_rule``, DR-30): a row a rule covers is *recorded*
with that rule's action, citing the rule pinned to its version; a row no rule
covers (one flagged ``needs_review``) is an exception, *gated* with no
proposal, and the rules gate decides it.

Every submission addresses the rules gate — recorded ones too, so a recorded
row can later be re-decided by the gate offline (shadow evaluation) — and
names the rule set it is decided under (``RULESET_ID``) in its gate context:
the binding carries no configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observation import AttributionSummary, GateContext, LabelType, ReasonerContext
from core.submission import BypassCitation, Submission, matching_bypass_rule
from mock_batch_reasoner.registry import MOCK_BATCH_REGISTRATION, RULESET_ID
from mock_batch_reasoner.scorer import SCORER_VERSION

if TYPE_CHECKING:
    from core.observation import ReasonerRegistration
    from mock_batch_reasoner.rows import AccountDay
    from mock_batch_reasoner.scorer import DelinquencyScore

#: The gate the mock's registration binds.
RULES_GATE_ID = "rules"


def reasoner_context(score: DelinquencyScore) -> ReasonerContext:
    """The scorer's evidence as the ledger's ``ReasonerContext``."""
    return ReasonerContext(
        reasoner_id=MOCK_BATCH_REGISTRATION.reasoner_id,
        reasoner_name=MOCK_BATCH_REGISTRATION.reasoner_name,
        model_version=SCORER_VERSION,
        label_type=LabelType.NUMERICAL,
        label_name="delinquency_score",
        label_value=score.delinquency_score,
        feature_set=dict(score.feature_set),
        attribution=AttributionSummary(feature_contributions=score.contributions),
    )


def rules_gate_context() -> GateContext:
    """The rules gate's context: the rule set every row is decided under."""
    return GateContext(gate_id=RULES_GATE_ID, gate_config={"ruleset_id": RULESET_ID})


def build_submission(
    row: AccountDay,
    score: DelinquencyScore,
    *,
    registration: ReasonerRegistration = MOCK_BATCH_REGISTRATION,
) -> Submission:
    """Assemble the mock's ``Submission`` for one scored row.

    Args:
        row: The account's end-of-day position.
        score: Its score.
        registration: The registration whose bypass rules pick the mode.

    Returns:
        A ``record`` submission (the rule's action and a version-pinned
        citation) when a bypass rule holds; otherwise a ``gate`` submission
        with no proposal (an exception). Both address the rules gate, name
        the account as their native subject, and carry the row as
        ``raw_event``.
    """
    context = reasoner_context(score)
    rule = matching_bypass_rule(registration, context)
    return Submission(
        reasoner_id=registration.reasoner_id,
        event_id=row.event_id,
        timestamp=row.as_of,
        entity_id=row.entity_id,
        entity_type=row.entity_type,
        native_subject=row.native_subject,
        mode="record" if rule is not None else "gate",
        proposed_action=rule.action if rule is not None else None,
        bypass=(
            BypassCitation(rule_id=rule.rule_id, rule_version=rule.version)
            if rule is not None
            else None
        ),
        reasoner_context=context,
        gate_contexts=(rules_gate_context(),),
        raw_event=row.payload(),
    )
