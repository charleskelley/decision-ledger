"""Framework action vocabulary — final resolved states a decision can take."""

from enum import StrEnum


class DecisionAction(StrEnum):
    """Final action the enforcement layer produces for a governed decision.

    Members are declared in ascending severity: ALLOW, CHALLENGE, HOLD, BLOCK.
    Severity ordering is an enforcement concern, not a property of this enum.
    The enforcement resolver (``app.enforcement.resolver``) defines and owns
    the authoritative severity ranking via ``_SEVERITY_ORDER``.

    .. warning::
        ``StrEnum`` comparison is lexicographic — ``BLOCK < CHALLENGE``
        alphabetically. Never use ``<`` / ``>`` on ``DecisionAction`` to
        determine severity.

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
