"""The stream mock's assembler — a scored transfer into a ``Submission``.

The handoff boundary between the mock's domain and the ledger: the transfer
crosses only as opaque ``raw_event`` JSON, and the score only as the
``ReasonerContext`` (with the complete feature snapshot the scorer read from
the account's sliding state, so replay and the bypass rules need nothing
else). The mode comes from the registration's bypass rules
(``matching_bypass_rule``, DR-30): a score in a band is *recorded* with that
band's action, citing the rule pinned to its version; any other score asks
the ledger to *gate* the transfer, with no proposal.

Every submission addresses the policy gate — recorded ones too, so a
recorded decision can later be re-decided by the gate offline (shadow
evaluation). The gate context names the prompt template the mock ships
(``assets/prompts/mock-stream-v1.yaml``, which renders the evidence by
authority tier with the conflict/gap protocol of DR-34), renders its
variables, and carries the retrieval query and jurisdiction filter: the
policy gate retrieves over the corpus the binding names
(``mock-stream-practices``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observation import AttributionSummary, GateContext, LabelType, ReasonerContext
from core.submission import BypassCitation, Submission, matching_bypass_rule
from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION
from mock_stream_reasoner.scorer import SCORER_VERSION

if TYPE_CHECKING:
    from pydantic import JsonValue

    from core.observation import ReasonerRegistration
    from mock_stream_reasoner.events import TransferEvent
    from mock_stream_reasoner.scorer import ActivityScore

#: The prompt template the mock's gate context names.
TEMPLATE_ID = "mock-stream-v1"
#: The jurisdiction every document of the mock's corpus applies in.
GLOBAL_JURISDICTION = "GLOBAL"


def reasoner_context(score: ActivityScore) -> ReasonerContext:
    """The scorer's evidence as the ledger's ``ReasonerContext``."""
    return ReasonerContext(
        reasoner_id=MOCK_STREAM_REGISTRATION.reasoner_id,
        reasoner_name=MOCK_STREAM_REGISTRATION.reasoner_name,
        model_version=SCORER_VERSION,
        label_type=LabelType.NUMERICAL,
        label_name="activity_score",
        label_value=score.activity_score,
        feature_set=dict(score.feature_set),
        attribution=AttributionSummary(feature_contributions=score.contributions),
    )


def retrieval_query(score: ActivityScore) -> str:
    """The query the policy gate retrieves the mock's practice notes with."""
    features = score.features
    history = (
        "a new account with a thin transfer history"
        if features.thin_history
        else f"an account with {features.prior_events} prior transfers"
    )
    return (
        f"transfer review for {history}: amount {features.amount:.2f} is "
        f"{features.amount_ratio:.1f} times the account's baseline, "
        f"{features.window_events} transfers in the last ten minutes; "
        "review hold, confirmation, velocity, new account practice"
    )


def policy_gate_context(score: ActivityScore) -> GateContext:
    """The policy gate's context for one scored transfer.

    Args:
        score: The transfer's score and features.

    Returns:
        ``gate_id="policy"`` with the template, its variables, the retrieval
        query, and the jurisdiction filter.
    """
    features = score.features
    gate_config: dict[str, JsonValue] = {
        "template_id": TEMPLATE_ID,
        "template_vars": {
            "activity_score": f"{score.activity_score:.3f}",
            "amount": f"{features.amount:.2f}",
            "baseline_amount": f"{features.baseline_amount:.2f}",
            "amount_ratio": f"{features.amount_ratio:.2f}",
            "window_events": str(features.window_events),
            "prior_events": str(features.prior_events),
            "thin_history": str(features.thin_history),
        },
        "retrieval_query": retrieval_query(score),
        "jurisdictions": [GLOBAL_JURISDICTION],
    }
    return GateContext(gate_id="policy", gate_config=gate_config)


def build_submission(
    event: TransferEvent,
    score: ActivityScore,
    *,
    registration: ReasonerRegistration = MOCK_STREAM_REGISTRATION,
) -> Submission:
    """Assemble the mock's ``Submission`` for one scored transfer.

    Args:
        event: The transfer.
        score: Its score, from the account's state.
        registration: The registration whose bypass rules pick the mode.

    Returns:
        A ``record`` submission (the band's action and a version-pinned
        citation) when a bypass rule holds; otherwise a ``gate`` submission
        with no proposal. Both address the policy gate, name the account as
        their native subject, and carry the transfer as ``raw_event``.
    """
    context = reasoner_context(score)
    rule = matching_bypass_rule(registration, context)
    return Submission(
        reasoner_id=registration.reasoner_id,
        event_id=event.event_id,
        timestamp=event.timestamp,
        entity_id=event.entity_id,
        entity_type=event.entity_type,
        native_subject=event.native_subject,
        mode="record" if rule is not None else "gate",
        proposed_action=rule.action if rule is not None else None,
        bypass=(
            BypassCitation(rule_id=rule.rule_id, rule_version=rule.version)
            if rule is not None
            else None
        ),
        reasoner_context=context,
        gate_contexts=(policy_gate_context(score),),
        raw_event=event.model_dump(mode="json"),
    )
