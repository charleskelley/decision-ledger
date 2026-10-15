"""The conjunction combinator's algebra, checked exhaustively (DR-29).

``most_restrictive`` is a maximum over ALLOW < CHALLENGE < HOLD < BLOCK. The
properties the ledger relies on — order and parallelism cannot change a
result, no gate relaxes another, the proposal is a floor, more gates can
only keep or raise the result — are each checked over every combination of
actions up to the sizes below (four actions, so the products stay small).
"""

from __future__ import annotations

import itertools

import pytest

from core.actions import DecisionAction
from core.gate import SEVERITY_ORDER, most_restrictive, severity

ALLOW, CHALLENGE, HOLD, BLOCK = (
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
)
ACTIONS = tuple(DecisionAction)
PROPOSALS = (None, *ACTIONS)


def _combine(*actions: DecisionAction) -> DecisionAction:
    return most_restrictive(None, actions)


def test_severity_order_is_allow_challenge_hold_block():
    assert SEVERITY_ORDER == (ALLOW, CHALLENGE, HOLD, BLOCK)
    assert [severity(a) for a in SEVERITY_ORDER] == [0, 1, 2, 3]


def test_every_action_has_exactly_one_rank():
    assert sorted(SEVERITY_ORDER, key=severity) == list(SEVERITY_ORDER)
    assert set(SEVERITY_ORDER) == set(DecisionAction)


def test_combining_block_with_challenge_is_block_not_the_lexicographic_max():
    # StrEnum compares as strings: "CHALLENGE" > "BLOCK". The combinator
    # must use severity, never string order.
    assert max(BLOCK.value, CHALLENGE.value) == CHALLENGE.value
    assert _combine(CHALLENGE, BLOCK) == BLOCK


@pytest.mark.parametrize("action", ACTIONS)
def test_a_single_action_combines_to_itself(action):
    assert most_restrictive(None, [action]) == action
    assert most_restrictive(action, []) == action


def test_nothing_to_combine_raises():
    with pytest.raises(ValueError, match="needs a proposed or an enforced action"):
        most_restrictive(None, [])


@pytest.mark.parametrize(("a", "b"), list(itertools.product(ACTIONS, repeat=2)))
def test_combination_is_commutative(a, b):
    assert _combine(a, b) == _combine(b, a)


@pytest.mark.parametrize(("a", "b", "c"), list(itertools.product(ACTIONS, repeat=3)))
def test_combination_is_associative(a, b, c):
    assert _combine(_combine(a, b), c) == _combine(a, _combine(b, c))
    assert _combine(_combine(a, b), c) == _combine(a, b, c)


@pytest.mark.parametrize("action", ACTIONS)
def test_combination_is_idempotent(action):
    assert _combine(action, action) == action
    assert _combine(action, action, action) == action


@pytest.mark.parametrize("action", ACTIONS)
def test_no_proposal_is_the_identity(action):
    assert most_restrictive(None, [action]) == most_restrictive(action, [])


def test_every_ordering_of_every_gate_set_up_to_four_gates_agrees():
    # Evaluation order and parallelism (which only changes the order the
    # outputs arrive in) cannot change the result.
    for size in range(1, 5):
        for actions in itertools.product(ACTIONS, repeat=size):
            for proposed in PROPOSALS:
                expected = most_restrictive(proposed, actions)
                assert all(
                    most_restrictive(proposed, perm) == expected
                    for perm in itertools.permutations(actions)
                )


@pytest.mark.parametrize("proposed", ACTIONS)
def test_the_proposal_is_a_floor(proposed):
    for size in range(4):
        for actions in itertools.product(ACTIONS, repeat=size):
            assert severity(most_restrictive(proposed, actions)) >= severity(proposed)


def test_no_gate_can_relax_another():
    # Every enforced action is a floor too: the result is at least as
    # restrictive as each input.
    for size in range(1, 5):
        for actions in itertools.product(ACTIONS, repeat=size):
            result = _combine(*actions)
            assert all(severity(result) >= severity(a) for a in actions)


def test_adding_a_gate_can_only_keep_or_raise_the_result():
    # Monotone: the property that makes short-circuiting safe once the
    # running result is BLOCK.
    for proposed in PROPOSALS:
        for size in range(4):
            for actions in itertools.product(ACTIONS, repeat=size):
                if proposed is None and not actions:
                    continue
                before = most_restrictive(proposed, actions)
                for extra in ACTIONS:
                    after = most_restrictive(proposed, [*actions, extra])
                    assert severity(after) >= severity(before)


def test_once_the_result_is_block_no_further_gate_changes_it():
    for size in range(4):
        for actions in itertools.product(ACTIONS, repeat=size):
            assert most_restrictive(BLOCK, actions) == BLOCK
            assert _combine(*actions, BLOCK) == BLOCK


def test_the_result_is_always_one_of_the_inputs():
    for proposed in PROPOSALS:
        for size in range(1, 4):
            for actions in itertools.product(ACTIONS, repeat=size):
                result = most_restrictive(proposed, actions)
                assert result in {*actions, *([proposed] if proposed else [])}


def test_accepts_any_iterable_including_a_generator():
    assert most_restrictive(ALLOW, (a for a in (CHALLENGE, HOLD))) == HOLD
