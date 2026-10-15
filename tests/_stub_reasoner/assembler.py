"""The stub reasoner's assembler — its event and evidence into a ``Submission``.

What every reasoner does at the handoff, in miniature: pick the mode with
the framework's ``matching_bypass_rule`` (the strictest registered bypass
rule that holds, registration order breaking ties → record its action
citing it; none → ask for a gate), address every bound gate, and store the
event as opaque JSON.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observation import GateContext
from core.submission import BypassCitation, Submission, matching_bypass_rule
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.observation import ReasonerContext, ReasonerRegistration
    from tests._stub_reasoner.events import StubEvent


def build_submission(
    event: StubEvent,
    reasoner_context: ReasonerContext,
    *,
    gate_contexts: Sequence[GateContext] | None = None,
    registration: ReasonerRegistration = STUB_REGISTRATION,
) -> Submission:
    """Assemble a stub submission, recording when a bypass rule holds.

    Args:
        event: The stub event.
        reasoner_context: The stub scorer's evidence.
        gate_contexts: Gate contexts to send; defaults to an empty context
            for every gate ``registration`` binds.
        registration: The registration whose bypass rules pick the mode.

    Returns:
        A record citing the strictest bypass rule that holds, or a gate
        request with no proposal.
    """
    contexts = (
        tuple(gate_contexts)
        if gate_contexts is not None
        else tuple(GateContext(gate_id=b.gate_id) for b in registration.gates)
    )
    rule = matching_bypass_rule(registration, reasoner_context)
    return Submission(
        reasoner_id=registration.reasoner_id,
        event_id=event.event_id,
        timestamp=event.timestamp,
        entity_id=event.entity_id,
        entity_type=event.entity_type,
        mode="record" if rule is not None else "gate",
        proposed_action=rule.action if rule is not None else None,
        bypass=(
            BypassCitation(rule_id=rule.rule_id, rule_version=rule.version)
            if rule is not None
            else None
        ),
        reasoner_context=reasoner_context,
        gate_contexts=contexts,
        raw_event=event.model_dump(mode="json"),
    )
