"""Behavioral tests for DecisionAction vocabulary.

DecisionAction is a pure StrEnum — it defines what actions exist, not how
they rank. Severity ordering is an enforcement concern, tested in
tests/app/test_enforcement.py.
"""

from core.actions import DecisionAction


def test_all_four_actions_exist():
    assert DecisionAction.ALLOW == "ALLOW"
    assert DecisionAction.CHALLENGE == "CHALLENGE"
    assert DecisionAction.HOLD == "HOLD"
    assert DecisionAction.BLOCK == "BLOCK"


def test_decision_action_is_usable_as_string():
    assert f"action={DecisionAction.ALLOW}" == "action=ALLOW"
    assert DecisionAction("BLOCK") == DecisionAction.BLOCK
