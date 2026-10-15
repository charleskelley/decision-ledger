"""Framework action vocabulary — final resolved states a decision can take."""

from enum import StrEnum


class DecisionAction(StrEnum):
    """Final action the enforcement layer produces for a governed decision.

    Members are declared in ascending severity: ALLOW, CHALLENGE, HOLD, BLOCK.
    Severity ordering is not a property of this enum: the conjunction
    combinator (``core.gate.combinator``) owns the authoritative ranking,
    ``SEVERITY_ORDER``, which enforcement and the combinator both read.

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
