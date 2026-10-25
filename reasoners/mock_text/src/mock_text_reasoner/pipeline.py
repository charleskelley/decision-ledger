"""The text mock's pipeline — classify a message, submit it, read the decision.

One message at a time, with no state between them: the classifier labels
the text, the assembler builds a gate-mode ``Submission`` proposing the
label's action, and a ``LedgerClient`` submits it (``HttpClient`` from the
CLI; ``LocalClient`` in the eval driver and the tests, which run the same
operation the HTTP route runs). ``decision_report`` reads back what the
ledger decided, for the CLI and the tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.enforcement.resolver import credited_rule
from mock_text_reasoner.assembler import POLICY_GATE_ID, build_submission
from mock_text_reasoner.classifier import classify

if TYPE_CHECKING:
    from app.api.models import DecisionReceipt
    from app.client import LedgerClient
    from core.bundle import DecisionBundle
    from core.submission import Submission
    from mock_text_reasoner.messages import CustomerMessage

#: The policy gate's ``extra`` keys the report repeats when present: the
#: evidence protocol's findings and the hold it caused (DR-34).
PROTOCOL_KEYS = (
    "conflict_detected",
    "evidence_sufficient",
    "governing_corpus_id",
    "protocol_hold",
)


async def decide_message(
    message: CustomerMessage, *, client: LedgerClient
) -> tuple[DecisionReceipt, Submission]:
    """Label one message and ask the ledger to decide it.

    Args:
        message: The customer's message.
        client: The ledger.

    Returns:
        The ledger's receipt and the submission it decided.

    Raises:
        core.exceptions.LedgerError: The ledger refused the submission.
    """
    submission = build_submission(message, classify(message.text))
    receipt = await client.submit(submission)
    return receipt, submission


def decision_report(bundle: DecisionBundle) -> dict[str, Any]:
    """What the ledger decided about one message, as a JSON-shaped report.

    Args:
        bundle: The decision's bundle.

    Returns:
        The bundle id, the label and proposal the mock sent, the final action
        and the rule credited with it, and the policy gate's part: whether it
        ran, its permitted actions, confidence and escalation, the evidence
        protocol's findings from its ``extra`` (``protocol_hold`` is ``None``
        when the gate held nothing back), and the sources of its evidence.
    """
    submission = bundle.submission
    record = bundle.gate(POLICY_GATE_ID)
    output = record.output if record is not None else None
    verdict = output.verdict if output is not None else None
    extra = output.extra if output is not None else {}
    return {
        "decision_id": bundle.decision_id,
        "event_id": submission.event_id,
        "label": submission.reasoner_context.label_value,
        "proposed_action": (
            submission.proposed_action.value
            if submission.proposed_action is not None
            else None
        ),
        "decision_action": bundle.decision_action.value,
        "decision_level_rule": bundle.decision_level_rule,
        "enforcement_rule_applied": credited_rule(bundle),
        "policy": {
            "status": record.status if record is not None else None,
            "permitted_actions": (
                [action.value for action in verdict.permitted_actions]
                if verdict is not None
                else None
            ),
            "confidence": verdict.confidence if verdict is not None else None,
            "escalate_to_human": (
                verdict.escalate_to_human if verdict is not None else None
            ),
            **{key: extra.get(key) for key in PROTOCOL_KEYS},
            "evidence_sources": sorted(
                {ref.source_id for ref in output.evidence} if output is not None else ()
            ),
        },
    }
