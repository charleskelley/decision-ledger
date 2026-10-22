"""The example's assembler — a scored inspection into a ``Submission``.

The handoff boundary between the example domain and the ledger: the widget
event crosses only as opaque ``raw_event`` JSON, and the score only as the
``ReasonerContext``. The mode comes from the registration's bypass rules
(``matching_bypass_rule``, DR-30): a score in a band is *recorded* with that
band's action, citing the rule pinned to its version; any other score asks
the ledger to *gate* the widget, with no proposal.

Every submission addresses the policy gate — recorded ones too, so any
recorded decision can later be re-decided by the gate offline (shadow
evaluation). The gate context names the prompt template the example ships
(``prompts/example-v2.yaml``, which renders the evidence by authority tier
with the conflict/gap protocol of DR-34; ``example-v1`` stays selectable),
renders its variables, and carries the retrieval query and jurisdiction
filter: the policy gate runs retrieval itself (DR-28), over the corpus the
example's binding names (``example-policy``, its three documents).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observation import AttributionSummary, GateContext, LabelType, ReasonerContext
from core.submission import BypassCitation, Submission, matching_bypass_rule
from example_reasoner.registry import EXAMPLE_REGISTRATION
from example_reasoner.scorer import SCORER_VERSION

if TYPE_CHECKING:
    from pydantic import JsonValue

    from core.observation import ReasonerRegistration
    from example_reasoner.events import WidgetEvent
    from example_reasoner.scorer import WidgetScore

#: The prompt template the example's gate context names (DR-34's protocol).
TEMPLATE_ID = "example-v2"
#: Jurisdiction of the corpus document that applies in every region.
GLOBAL_JURISDICTION = "GLOBAL"


def reasoner_context(score: WidgetScore) -> ReasonerContext:
    """The scorer's evidence as the ledger's ``ReasonerContext``."""
    return ReasonerContext(
        reasoner_id=EXAMPLE_REGISTRATION.reasoner_id,
        reasoner_name=EXAMPLE_REGISTRATION.reasoner_name,
        model_version=SCORER_VERSION,
        label_type=LabelType.NUMERICAL,
        label_name="widget_score",
        label_value=score.widget_score,
        feature_set=dict(score.feature_set),
        attribution=AttributionSummary(feature_contributions=score.contributions),
    )


def policy_gate_context(event: WidgetEvent, score: WidgetScore) -> GateContext:
    """The policy gate's context for one inspection.

    Args:
        event: The inspection.
        score: Its score.

    Returns:
        ``gate_id="policy"`` with the template, its variables, the retrieval
        query, and the jurisdictions (the inspection's region and the global
        standard).
    """
    gate_config: dict[str, JsonValue] = {
        "template_id": TEMPLATE_ID,
        "template_vars": {
            "widget_score": f"{score.widget_score:.3f}",
            "region": event.region,
            "wobble": f"{event.wobble:.2f}",
            "misalignment_mm": f"{event.misalignment_mm:.1f}",
            "prior_inspections": str(event.prior_inspections),
            "thin_history": str(score.feature_set["thin_history"]),
        },
        "retrieval_query": (
            f"widget inspection in {event.region}: wobble {event.wobble:.2f}, "
            f"gizmo misalignment {event.misalignment_mm:.1f} mm, "
            f"{event.prior_inspections} prior inspections; release criteria, "
            "calibration review, sprocket inspection, escalation"
        ),
        "jurisdictions": [event.region, GLOBAL_JURISDICTION],
    }
    return GateContext(gate_id="policy", gate_config=gate_config)


def build_submission(
    event: WidgetEvent,
    score: WidgetScore,
    *,
    registration: ReasonerRegistration = EXAMPLE_REGISTRATION,
) -> Submission:
    """Assemble the example's ``Submission`` for one scored inspection.

    Args:
        event: The inspection.
        score: Its score.
        registration: The registration whose bypass rules pick the mode.

    Returns:
        A ``record`` submission (the band's action and a version-pinned
        citation) when a bypass rule holds; otherwise a ``gate`` submission
        with no proposal. Both address the policy gate, name the widget as
        their native subject, and carry the event as ``raw_event``.
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
        gate_contexts=(policy_gate_context(event, score),),
        raw_event=event.model_dump(mode="json"),
    )
