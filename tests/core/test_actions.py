"""Behavioral tests for DecisionAction contracts.

Key contract: action severity is ordered ALLOW < CHALLENGE < HOLD < BLOCK.
StrEnum uses lexicographic string comparison — NOT severity order. The
enforcement layer must use SEVERITY_ORDER for conservative action resolution,
never relying on < > operators directly on DecisionAction values.
"""

from core.actions import DecisionAction

# The authoritative severity ordering. Any code that resolves "most
# conservative action" must use this — not string comparison.
SEVERITY_ORDER = [
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
]


def test_all_four_actions_exist():
    assert DecisionAction.ALLOW == "ALLOW"
    assert DecisionAction.CHALLENGE == "CHALLENGE"
    assert DecisionAction.HOLD == "HOLD"
    assert DecisionAction.BLOCK == "BLOCK"


def test_severity_order_is_allow_challenge_hold_block():
    assert SEVERITY_ORDER.index(DecisionAction.ALLOW) == 0
    assert SEVERITY_ORDER.index(DecisionAction.CHALLENGE) == 1
    assert SEVERITY_ORDER.index(DecisionAction.HOLD) == 2
    assert SEVERITY_ORDER.index(DecisionAction.BLOCK) == 3


def test_most_conservative_action_selected_from_permitted_set():
    # Enforcement resolves to the most conservative permissible action.
    assert (
        max([DecisionAction.ALLOW, DecisionAction.CHALLENGE], key=SEVERITY_ORDER.index)
        == DecisionAction.CHALLENGE
    )
    assert (
        max([DecisionAction.CHALLENGE, DecisionAction.HOLD], key=SEVERITY_ORDER.index)
        == DecisionAction.HOLD
    )
    assert (
        max([DecisionAction.ALLOW, DecisionAction.BLOCK], key=SEVERITY_ORDER.index)
        == DecisionAction.BLOCK
    )


def test_string_comparison_does_not_reflect_severity():
    # StrEnum compares lexicographically. BLOCK < CHALLENGE alphabetically,
    # but CHALLENGE < BLOCK by severity. Never use < > on DecisionAction.
    assert DecisionAction.BLOCK < DecisionAction.CHALLENGE  # alphabetical
    assert (  # severity
        SEVERITY_ORDER.index(DecisionAction.BLOCK)
        > SEVERITY_ORDER.index(DecisionAction.CHALLENGE)
    )


def test_decision_action_is_usable_as_string():
    assert f"action={DecisionAction.ALLOW}" == "action=ALLOW"
    assert DecisionAction("BLOCK") == DecisionAction.BLOCK
