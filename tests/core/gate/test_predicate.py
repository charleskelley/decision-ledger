"""Behavioral tests for the predicate language (``core.gate.predicate``).

The predicate language is shared by the rules gate and, from the submission
contract on, by registered bypass rules — so these tests pin it exhaustively:
operator semantics, kind separation (booleans are not numbers, strings are
not numbers), short-circuit evaluation, fail-closed errors, node validation,
and the JSON round trip that lets a predicate live in a YAML rule file or a
registration record.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.exceptions import PredicateError
from core.gate import (
    AllOf,
    AnyOf,
    Compare,
    Not,
    evaluate_predicate,
    parse_predicate,
    referenced_fields,
)
from core.gate.predicate import resolve_field
from core.observation import LabelType, ReasonerContext


def _subject(
    *,
    label_value: float | str = 0.15,
    feature_set: dict[str, float | int | str | bool] | None = None,
) -> ReasonerContext:
    return ReasonerContext(
        reasoner_id="stub-reasoner",
        reasoner_name="Stub Reasoner",
        model_version="stub-scorer-v1",
        label_type=(
            LabelType.NUMERICAL
            if isinstance(label_value, float)
            else LabelType.CATEGORICAL
        ),
        label_name="widget_score",
        label_value=label_value,
        feature_set=(
            feature_set
            if feature_set is not None
            else {
                "wobble": 3,
                "ratio": 0.5,
                "thin_history": True,
                "region": "REGION_A",
                "speed": "N/A",
            }
        ),
    )


def _holds(predicate, subject=None) -> bool:
    return evaluate_predicate(predicate, subject or _subject())


# ---------------------------------------------------------------------------
# Compare — numeric operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("lt", 0.2, True),
        ("lt", 0.15, False),
        ("le", 0.15, True),
        ("le", 0.1, False),
        ("gt", 0.1, True),
        ("gt", 0.15, False),
        ("ge", 0.15, True),
        ("ge", 0.2, False),
        ("eq", 0.15, True),
        ("eq", 0.16, False),
        ("ne", 0.16, True),
        ("ne", 0.15, False),
    ],
)
def test_numeric_operators_on_label_value(op, value, expected):
    assert _holds(Compare(field="label_value", op=op, value=value)) is expected


def test_numeric_operators_read_feature_set_values():
    assert _holds(Compare(field="feature_set.wobble", op="gt", value=2))
    assert not _holds(Compare(field="feature_set.wobble", op="gt", value=3))


def test_int_and_float_compare_as_numbers():
    assert _holds(Compare(field="feature_set.wobble", op="eq", value=3.0))
    assert _holds(Compare(field="feature_set.ratio", op="lt", value=1))


def test_an_integer_too_large_for_a_float_orders_exactly():
    subject = _subject(feature_set={"count": 10**400})
    assert _holds(Compare(field="feature_set.count", op="gt", value=5), subject)
    assert _holds(Compare(field="feature_set.count", op="ge", value=1e308), subject)
    assert not _holds(Compare(field="feature_set.count", op="lt", value=0.5), subject)


# ---------------------------------------------------------------------------
# Compare — kind separation
# ---------------------------------------------------------------------------


def test_a_boolean_never_equals_a_number():
    # Python says True == 1; the predicate language does not.
    assert not _holds(Compare(field="feature_set.thin_history", op="eq", value=1))
    assert _holds(Compare(field="feature_set.thin_history", op="ne", value=1))


def test_a_number_never_equals_a_boolean():
    subject = _subject(feature_set={"count": 1})
    assert not _holds(Compare(field="feature_set.count", op="eq", value=True), subject)


def test_booleans_compare_with_booleans():
    assert _holds(Compare(field="feature_set.thin_history", op="eq", value=True))
    assert not _holds(Compare(field="feature_set.thin_history", op="eq", value=False))


def test_strings_compare_with_strings_only():
    assert _holds(Compare(field="feature_set.region", op="eq", value="REGION_A"))
    assert not _holds(Compare(field="feature_set.speed", op="eq", value=0.0))
    assert _holds(Compare(field="feature_set.speed", op="ne", value=0.0))


def test_a_categorical_label_compares_by_equality():
    subject = _subject(label_value="NEGATIVE")
    assert _holds(Compare(field="label_value", op="eq", value="NEGATIVE"), subject)


# ---------------------------------------------------------------------------
# Compare — membership
# ---------------------------------------------------------------------------


def test_in_holds_when_the_value_is_listed():
    predicate = Compare(field="feature_set.region", op="in", value=["REGION_A", "X"])
    assert _holds(predicate)


def test_not_in_holds_when_the_value_is_absent():
    predicate = Compare(field="feature_set.region", op="not_in", value=["REGION_B"])
    assert _holds(predicate)


def test_membership_keeps_booleans_and_numbers_apart():
    predicate = Compare(field="feature_set.thin_history", op="in", value=[1, 0])
    assert not _holds(predicate)


# ---------------------------------------------------------------------------
# Fail closed — the evaluator refuses to guess
# ---------------------------------------------------------------------------


def test_a_missing_feature_raises_predicate_error():
    with pytest.raises(PredicateError, match="no key 'absent'"):
        _holds(Compare(field="feature_set.absent", op="eq", value=1))


def test_ordering_a_non_number_raises_predicate_error():
    with pytest.raises(PredicateError, match="needs a number"):
        _holds(Compare(field="feature_set.speed", op="gt", value=900))


def test_ordering_a_boolean_raises_predicate_error():
    with pytest.raises(PredicateError, match="needs a number"):
        _holds(Compare(field="feature_set.thin_history", op="lt", value=2))


def test_ordering_a_categorical_label_raises_predicate_error():
    subject = _subject(label_value="NEGATIVE")
    with pytest.raises(PredicateError):
        _holds(Compare(field="label_value", op="lt", value=0.5), subject)


# ---------------------------------------------------------------------------
# Combinators
# ---------------------------------------------------------------------------

_TRUE = Compare(field="label_value", op="lt", value=0.2)
_FALSE = Compare(field="label_value", op="gt", value=0.2)
_RAISES = Compare(field="feature_set.absent", op="eq", value=1)


@pytest.mark.parametrize(
    ("children", "expected"),
    [([_TRUE], True), ([_TRUE, _TRUE], True), ([_TRUE, _FALSE], False)],
)
def test_all_of_holds_when_every_child_holds(children, expected):
    assert _holds(AllOf(predicates=children)) is expected


@pytest.mark.parametrize(
    ("children", "expected"),
    [([_FALSE], False), ([_FALSE, _TRUE], True), ([_FALSE, _FALSE], False)],
)
def test_any_of_holds_when_some_child_holds(children, expected):
    assert _holds(AnyOf(predicates=children)) is expected


def test_not_negates_its_child():
    assert _holds(Not(predicate=_FALSE))
    assert not _holds(Not(predicate=_TRUE))


def test_all_of_short_circuits_left_to_right():
    # A guard first makes an otherwise-failing comparison safe.
    guarded = AllOf(
        predicates=[
            Compare(field="feature_set.speed", op="ne", value="N/A"),
            Compare(field="feature_set.speed", op="gt", value=900),
        ]
    )
    assert not _holds(guarded)
    assert not _holds(AllOf(predicates=[_FALSE, _RAISES]))


def test_any_of_short_circuits_left_to_right():
    assert _holds(AnyOf(predicates=[_TRUE, _RAISES]))


def test_errors_inside_combinators_propagate():
    with pytest.raises(PredicateError):
        _holds(AllOf(predicates=[_TRUE, _RAISES]))
    with pytest.raises(PredicateError):
        _holds(Not(predicate=_RAISES))


def test_nested_trees_evaluate():
    tree = AllOf(
        predicates=[
            AnyOf(
                predicates=[
                    _FALSE,
                    Compare(field="feature_set.wobble", op="ge", value=3),
                ]
            ),
            Not(
                predicate=Compare(
                    field="feature_set.region", op="in", value=["REGION_B"]
                )
            ),
        ]
    )
    assert _holds(tree)


# ---------------------------------------------------------------------------
# Node validation — malformed predicates never construct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "compare", "field": "score", "op": "eq", "value": 1},
        {"kind": "compare", "field": "feature_set.", "op": "eq", "value": 1},
        {"kind": "compare", "field": "label_value", "op": "lt", "value": "high"},
        {"kind": "compare", "field": "label_value", "op": "gt", "value": True},
        {"kind": "compare", "field": "label_value", "op": "in", "value": 0.5},
        {"kind": "compare", "field": "label_value", "op": "in", "value": []},
        {"kind": "compare", "field": "label_value", "op": "eq", "value": [0.5]},
        {"kind": "compare", "field": "label_value", "op": "approx", "value": 0.5},
        {"kind": "compare", "field": "label_value", "op": "eq", "value": 1, "x": 2},
        {"kind": "all", "predicates": []},
        {"kind": "any", "predicates": []},
        {"kind": "xor", "predicates": []},
        {"field": "label_value", "op": "eq", "value": 1},
    ],
)
def test_malformed_predicates_are_rejected(data):
    with pytest.raises(ValidationError):
        parse_predicate(data)


def test_predicates_are_frozen():
    predicate = Compare(field="label_value", op="eq", value=1)
    with pytest.raises(ValidationError):
        predicate.value = 2


# ---------------------------------------------------------------------------
# Serialization and introspection
# ---------------------------------------------------------------------------


def test_a_predicate_round_trips_through_json():
    tree = AllOf(
        predicates=[
            Compare(field="label_value", op="lt", value=0.2),
            AnyOf(
                predicates=[
                    Compare(field="feature_set.region", op="in", value=["A", "B"]),
                    Not(
                        predicate=Compare(
                            field="feature_set.thin_history", op="eq", value=True
                        )
                    ),
                ]
            ),
        ]
    )
    assert parse_predicate(json.loads(json.dumps(tree.model_dump(mode="json")))) == tree


def test_parse_predicate_reads_the_documented_json_shape():
    predicate = parse_predicate(
        {
            "kind": "all",
            "predicates": [
                {"kind": "compare", "field": "label_value", "op": "lt", "value": 0.2},
                {
                    "kind": "compare",
                    "field": "feature_set.region",
                    "op": "in",
                    "value": ["REGION_A", "REGION_B"],
                },
            ],
        }
    )
    assert isinstance(predicate, AllOf)
    assert _holds(predicate)


def test_referenced_fields_collects_every_address_in_the_tree():
    tree = AllOf(
        predicates=[
            _TRUE,
            AnyOf(predicates=[Compare(field="feature_set.a", op="eq", value=1)]),
            Not(predicate=Compare(field="feature_set.b", op="eq", value=1)),
        ]
    )
    assert referenced_fields(tree) == frozenset(
        {"label_value", "feature_set.a", "feature_set.b"}
    )


def test_resolve_field_returns_the_subjects_value():
    subject = _subject()
    assert resolve_field("label_value", subject) == 0.15
    assert resolve_field("feature_set.region", subject) == "REGION_A"
    with pytest.raises(PredicateError):
        resolve_field("feature_set.absent", subject)
