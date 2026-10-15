"""The conjunction combinator — how a gate set's actions become one (DR-29).

A decision may pass through several gates. The ledger combines their
enforced actions, and the reasoner's proposal, with one fixed rule: the
final action is the **most restrictive** of them, over the order

    ALLOW < CHALLENGE < HOLD < BLOCK

``most_restrictive`` is a maximum over that order, so it is commutative,
associative, and idempotent, and a missing proposal (``None``) is its
identity. Evaluation order and parallelism therefore cannot change a result,
no gate can relax another, and adding a gate's action can only keep or raise
the result (monotone) — which is what makes skipping the remaining advisory
gates safe once the result is already ``BLOCK``.

The combinator is fixed (DR-29 rejects configurable combinators). Rules
that are not a plain maximum — the decision-level novel-entity tier, which
holds advisory verdicts for review — are enforcement's, applied before the
actions reach this function.

This module also names the three states a gate can end a decision in
(``GateStatus``), which every bundle records per gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from core.actions import DecisionAction

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Decision actions in ascending severity. The single authoritative ranking:
#: enforcement's "most conservative permitted action" and the combinator
#: both read it.
SEVERITY_ORDER: tuple[DecisionAction, ...] = (
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
)

_RANK = {action: rank for rank, action in enumerate(SEVERITY_ORDER)}

#: How a gate ended for one decision: it returned an output (``ran``), the
#: ledger did not invoke it (``skipped``), or it produced no usable output —
#: an error, a timeout, a malformed output, or no gate composed under its id
#: (``failed``, which enforcement treats as a missing verdict: fail closed).
GateStatus = Literal["ran", "skipped", "failed"]


def severity(action: DecisionAction) -> int:
    """Return the action's rank in ``SEVERITY_ORDER`` (ALLOW is 0, BLOCK is 3).

    Args:
        action: A decision action.

    Returns:
        Its zero-based severity rank.
    """
    return _RANK[action]


def most_restrictive(
    proposed: DecisionAction | None,
    enforced: Iterable[DecisionAction],
) -> DecisionAction:
    """Combine a proposal and enforced actions into the most restrictive one.

    Args:
        proposed: The reasoner's proposed action, or ``None`` when it made
            no proposal (the identity: it never lowers or raises a result).
        enforced: The enforced actions to combine (one per gate, plus any
            decision-level floor), in any order.

    Returns:
        The maximum of ``proposed`` and ``enforced`` over ``SEVERITY_ORDER``.

    Raises:
        ValueError: There is nothing to combine: no proposal and no
            enforced action.
    """
    candidates = [*([proposed] if proposed is not None else []), *enforced]
    if not candidates:
        msg = "most_restrictive() needs a proposed or an enforced action"
        raise ValueError(msg)
    return max(candidates, key=severity)
