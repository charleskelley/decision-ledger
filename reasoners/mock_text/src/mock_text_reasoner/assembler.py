"""The text mock's assembler — a labeled message into a ``Submission``.

The handoff boundary between the mock's domain and the ledger: the message
crosses only as opaque ``raw_event`` JSON, and the label only as the
``ReasonerContext`` (a categorical label, with the complete features the
classifier read). The registration has no bypass rules, so every message is
a ``gate`` submission, and it carries the label's action as its
``proposed_action``: the ledger's final action is the more restrictive of
that proposal and the policy gate's verdict, so the proposal is a floor the
gate can raise and never lower.

The gate context names the prompt template the mock ships
(``assets/prompts/mock-text-v1.yaml``, which renders the evidence by
authority tier with the conflict/gap protocol of DR-34), renders its
variables (the message itself among them: the gate's model reads what the
customer wrote), and carries the retrieval query and jurisdiction filter.
The query is one of six fixed topics, the label's, plus the category terms
the text matched, never the text itself, so a message cannot write it: an
instruction in a message reaches retrieval only by holding a category term,
which changes the label and so selects another of the fixed topics. The
policy gate retrieves over both corpora the binding names
(``mock-text-regulatory`` at tier 1, ``mock-text-practice`` at tier 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observation import (
    AttributionSummary,
    Contribution,
    GateContext,
    LabelType,
    ReasonerContext,
)
from core.submission import Submission
from mock_text_reasoner.classifier import (
    CATEGORIES,
    CLASSIFIER_VERSION,
    LABEL_NAME,
    UNCLASSIFIED,
)
from mock_text_reasoner.registry import MOCK_TEXT_REGISTRATION

if TYPE_CHECKING:
    from pydantic import JsonValue

    from core.observation import ReasonerRegistration
    from mock_text_reasoner.classifier import TextLabel
    from mock_text_reasoner.messages import CustomerMessage

#: The prompt template the mock's gate context names.
TEMPLATE_ID = "mock-text-v1"
#: The jurisdiction every document of the mock's corpora applies in.
GLOBAL_JURISDICTION = "GLOBAL"
#: The policy gate's id.
POLICY_GATE_ID = "policy"

#: What the policy gate retrieves for each label: the topic of the passages
#: on point in both corpora, in their own words.
_RETRIEVAL_TOPICS = {
    "credential_request": (
        "a message asking for a PIN, password, passcode, or one-time code: "
        "credentials are never disclosed, refuse and close the conversation"
    ),
    "fraud_report": (
        "a customer reports fraud or a payment they did not make: hold the "
        "message for the fraud team, no automated reply"
    ),
    "account_change": (
        "a request to change contact details (address, phone number, email): "
        "step-up identity check before the change"
    ),
    "hardship": (
        "a customer states financial hardship (lost a job, cannot pay): "
        "automated hardship reply or review by a trained agent"
    ),
    "general_inquiry": (
        "a general question about opening hours, fees, statements, or "
        "branches: automated reply"
    ),
    UNCLASSIFIED.name: (
        "a message the assistant cannot place in a category: review by an agent"
    ),
}


def reasoner_context(label: TextLabel) -> ReasonerContext:
    """The classifier's evidence as the ledger's ``ReasonerContext``."""
    contributions = [
        Contribution(
            feature_name=f"hits_{category.name}",
            feature_value=len(label.matched_terms[category.name]),
            method="keyword_hits",
            value=float(len(label.matched_terms[category.name])),
        )
        for category in CATEGORIES
        if label.matched_terms[category.name]
    ]
    return ReasonerContext(
        reasoner_id=MOCK_TEXT_REGISTRATION.reasoner_id,
        reasoner_name=MOCK_TEXT_REGISTRATION.reasoner_name,
        model_version=CLASSIFIER_VERSION,
        label_type=LabelType.CATEGORICAL,
        label_name=LABEL_NAME,
        label_value=label.category.name,
        feature_set=label.feature_set,
        attribution=AttributionSummary(
            feature_contributions=contributions or None,
            narrative=label.narrative(),
        ),
    )


def retrieval_query(label: TextLabel) -> str:
    """The query the policy gate retrieves both corpora with.

    The label's fixed topic and the category terms the text matched; never
    the text itself.
    """
    topic = _RETRIEVAL_TOPICS[label.category.name]
    terms = f"; terms: {', '.join(label.terms)}" if label.terms else ""
    return f"customer message to the automated assistant, {topic}{terms}"


def policy_gate_context(message: CustomerMessage, label: TextLabel) -> GateContext:
    """The policy gate's context for one labeled message.

    Args:
        message: The customer's message.
        label: Its label.

    Returns:
        ``gate_id="policy"`` with the template, its variables, the retrieval
        query, and the jurisdiction filter.
    """
    gate_config: dict[str, JsonValue] = {
        "template_id": TEMPLATE_ID,
        "template_vars": {
            "message_text": message.text,
            "channel": message.channel,
            "category": label.category.name,
            "category_description": label.category.description,
            "matched_terms": ", ".join(label.terms) or "none",
            "proposed_action": label.proposed_action.value,
        },
        "retrieval_query": retrieval_query(label),
        "jurisdictions": [GLOBAL_JURISDICTION],
    }
    return GateContext(gate_id=POLICY_GATE_ID, gate_config=gate_config)


def build_submission(
    message: CustomerMessage,
    label: TextLabel,
    *,
    registration: ReasonerRegistration = MOCK_TEXT_REGISTRATION,
) -> Submission:
    """Assemble the mock's ``Submission`` for one labeled message.

    Args:
        message: The customer's message.
        label: Its label.
        registration: The registration the submission is made under.

    Returns:
        A ``gate`` submission proposing the label's action, addressing the
        policy gate, naming the customer as its native subject, and carrying
        the message as ``raw_event``.
    """
    return Submission(
        reasoner_id=registration.reasoner_id,
        event_id=message.event_id,
        timestamp=message.received_at,
        entity_id=message.entity_id,
        entity_type=message.entity_type,
        native_subject=message.native_subject,
        mode="gate",
        proposed_action=label.proposed_action,
        bypass=None,
        reasoner_context=reasoner_context(label),
        gate_contexts=(policy_gate_context(message, label),),
        raw_event=message.payload(),
    )
