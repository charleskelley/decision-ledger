"""GateRouting — fast-path triage outcomes before the policy gate is invoked.

Placed at the core/ top level so both core/observation/ and core/gate/ can
import it without creating a lateral subpackage dependency.
"""

from enum import StrEnum


class GateRouting(StrEnum):
    """Routing decision the domain reasoner provides to the DecisionLedger framework.

    High-confidence decisions bypass the policy gate. Ambiguous observations
    and novel entities are routed to the gate for full LLM policy reasoning.

    Attributes:
        FAST_PATH_ALLOW: The reasoner is confident the observation is low risk.
            The framework routes directly to ALLOW without invoking the
            policy gate.
        FAST_PATH_BLOCK: The reasoner is confident the observation is high risk.
            The framework routes directly to BLOCK without invoking the
            policy gate.
        ROUTE_TO_GATE: The reasoner's confidence is insufficient for a
            fast-path decision. The framework forwards the observation to
            the policy gate for full policy reasoning and citation.
    """

    FAST_PATH_ALLOW = "FAST_PATH_ALLOW"
    FAST_PATH_BLOCK = "FAST_PATH_BLOCK"
    ROUTE_TO_GATE = "ROUTE_TO_GATE"
