"""Framework action vocabulary — final resolved states a decision can take."""

from enum import StrEnum


class DecisionAction(StrEnum):
    """Final action the enforcement layer produces for a governed decision.

    Severity is ordered: ALLOW < CHALLENGE < HOLD < BLOCK. The enforcement
    layer resolves to the most conservative permissible action when the policy
    gate indicates multiple actions.

    Attributes:
        ALLOW: No friction. The request proceeds without additional controls.
        CHALLENGE: A friction control is applied before the request proceeds.
            The specific control is domain-defined (e.g., step-up
            authentication, CAPTCHA, manual approval gate).
        HOLD: The request is suspended pending asynchronous human review.
            No automated resolution occurs until the review is completed.
        BLOCK: The request is immediately rejected. No further pipeline
            processing occurs.
    """

    ALLOW = "ALLOW"
    CHALLENGE = "CHALLENGE"
    HOLD = "HOLD"
    BLOCK = "BLOCK"
