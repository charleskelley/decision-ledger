"""Predicate language — a small, closed, pure AST over a reasoner's evidence.

Shared by the rules gate (declarative gate rules, DR-28) and, from the
submission-contract phase on, by registered bypass rules (DR-30). There are
exactly four node kinds:

- ``Compare(field, op, value)`` — a leaf comparison.
- ``AllOf(predicates)`` — holds when every child holds.
- ``AnyOf(predicates)`` — holds when at least one child holds.
- ``Not(predicate)`` — holds when its child does not.

A ``field`` addresses the subject's ``label_value`` or one key of its
``feature_set`` (``"feature_set.<name>"``). A ``value`` is a JSON scalar, or
a non-empty list of scalars for ``in`` / ``not_in``. Every node is a strict,
frozen Pydantic model discriminated by ``kind``, so a predicate round-trips
through JSON and YAML unchanged::

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

Evaluation (``evaluate_predicate``) is a pure function: no ``eval()``, no
expression strings, no I/O. ``AllOf`` and ``AnyOf`` short-circuit left to
right, so a guard such as ``AllOf([Compare(f, "ne", "N/A"), Compare(f,
"gt", 900)])`` is safe. The evaluator refuses to guess: a field the subject
does not carry, or an ordering comparison whose operand is not a number,
raises ``PredicateError`` so the caller can fail closed. Booleans and numbers
are distinct kinds: ``True`` never equals ``1``.

The language is closed on purpose (DR-28). Anything richer is a rules-engine
feature, not a predicate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from core.exceptions import PredicateError

if TYPE_CHECKING:
    from collections.abc import Mapping

#: A JSON scalar a predicate compares against or reads from its subject.
Scalar = bool | int | float | str

#: Comparison operators. Ordering operators need numbers on both sides;
#: ``in`` / ``not_in`` need a non-empty list value.
CompareOp = Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "not_in"]

#: Field address of the subject's prediction.
LABEL_FIELD = "label_value"
#: Prefix of a field address into the subject's feature snapshot.
FEATURE_PREFIX = "feature_set."

_ORDERING_OPS = frozenset({"lt", "le", "gt", "ge"})
_MEMBERSHIP_OPS = frozenset({"in", "not_in"})


class PredicateSubject(Protocol):
    """What a predicate reads: a prediction and a feature snapshot.

    ``core.observation.ReasonerContext`` satisfies this structurally; the
    predicate module does not import it, so ``core/gate/`` stays free of the
    observation layer (DR-11).
    """

    @property
    def label_value(self) -> float | str:
        """The reasoner's prediction."""
        ...

    @property
    def feature_set(self) -> Mapping[str, Scalar]:
        """The feature values the reasoner's model consumed."""
        ...


def _is_number(value: object) -> bool:
    """Return True for ints and floats; booleans are not numbers here."""
    return isinstance(value, int | float) and not isinstance(value, bool)


class Compare(BaseModel):
    """Leaf node: compare one field of the subject with a constant.

    Attributes:
        kind: Discriminator, always ``"compare"``.
        field: ``"label_value"`` or ``"feature_set.<name>"``.
        op: One of ``eq``, ``ne``, ``lt``, ``le``, ``gt``, ``ge``, ``in``,
            ``not_in``.
        value: The constant. A number for ordering operators, a non-empty
            list of scalars for ``in`` / ``not_in``, any scalar otherwise.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kind: Literal["compare"] = "compare"
    field: str
    op: CompareOp
    value: Scalar | list[Scalar]

    @field_validator("field")
    @classmethod
    def _field_is_addressable(cls, field: str) -> str:
        """Only ``label_value`` and ``feature_set.<name>`` are addressable."""
        if field == LABEL_FIELD or (
            field.startswith(FEATURE_PREFIX) and len(field) > len(FEATURE_PREFIX)
        ):
            return field
        msg = (
            f"field must be {LABEL_FIELD!r} or '{FEATURE_PREFIX}<name>', got {field!r}"
        )
        raise ValueError(msg)

    @model_validator(mode="after")
    def _value_fits_op(self) -> Compare:
        """Reject operator/value pairs that could never evaluate."""
        if self.op in _MEMBERSHIP_OPS:
            if not isinstance(self.value, list) or not self.value:
                msg = f"op {self.op!r} needs a non-empty list value"
                raise ValueError(msg)
        elif isinstance(self.value, list):
            msg = f"op {self.op!r} needs a scalar value, got a list"
            raise ValueError(msg)
        elif self.op in _ORDERING_OPS and not _is_number(self.value):
            msg = f"op {self.op!r} needs a numeric value, got {self.value!r}"
            raise ValueError(msg)
        return self


class AllOf(BaseModel):
    """Conjunction: holds when every child predicate holds.

    Attributes:
        kind: Discriminator, always ``"all"``.
        predicates: Children, evaluated left to right with short-circuit.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kind: Literal["all"] = "all"
    predicates: list[Predicate] = Field(min_length=1)


class AnyOf(BaseModel):
    """Disjunction: holds when at least one child predicate holds.

    Attributes:
        kind: Discriminator, always ``"any"``.
        predicates: Children, evaluated left to right with short-circuit.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kind: Literal["any"] = "any"
    predicates: list[Predicate] = Field(min_length=1)


class Not(BaseModel):
    """Negation: holds when its child predicate does not.

    Attributes:
        kind: Discriminator, always ``"not"``.
        predicate: The negated child.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kind: Literal["not"] = "not"
    predicate: Predicate


#: Any predicate node, discriminated by ``kind``.
Predicate = Annotated[Compare | AllOf | AnyOf | Not, Field(discriminator="kind")]

AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()

_PREDICATE_ADAPTER: TypeAdapter[Predicate] = TypeAdapter(Predicate)


def parse_predicate(data: object) -> Predicate:
    """Validate a JSON-shaped object (e.g. loaded from YAML) into a predicate.

    Args:
        data: A mapping in the node shape documented in this module.

    Returns:
        The validated predicate tree.

    Raises:
        pydantic.ValidationError: ``data`` is not a well-formed predicate.
    """
    return _PREDICATE_ADAPTER.validate_python(data)


def evaluate_predicate(predicate: Predicate, subject: PredicateSubject) -> bool:
    """Evaluate ``predicate`` against ``subject``. Pure and deterministic.

    Args:
        predicate: A validated predicate tree.
        subject: Anything with ``label_value`` and ``feature_set`` (a
            ``ReasonerContext``, typically).

    Returns:
        ``True`` when the predicate holds.

    Raises:
        PredicateError: A ``Compare`` names a feature the subject does not
            carry, or an ordering comparison meets a non-numeric operand.
    """
    if isinstance(predicate, Compare):
        return _compare(predicate, _resolve(predicate.field, subject))
    if isinstance(predicate, AllOf):
        return all(evaluate_predicate(child, subject) for child in predicate.predicates)
    if isinstance(predicate, AnyOf):
        return any(evaluate_predicate(child, subject) for child in predicate.predicates)
    return not evaluate_predicate(predicate.predicate, subject)


def referenced_fields(predicate: Predicate) -> frozenset[str]:
    """Return every field address the predicate reads.

    Args:
        predicate: A validated predicate tree.

    Returns:
        The set of ``label_value`` / ``feature_set.<name>`` addresses, which
        lets a caller record exactly the observed values a rule depended on.
    """
    if isinstance(predicate, Compare):
        return frozenset({predicate.field})
    if isinstance(predicate, AllOf | AnyOf):
        return frozenset().union(*(referenced_fields(c) for c in predicate.predicates))
    return referenced_fields(predicate.predicate)


def resolve_field(field: str, subject: PredicateSubject) -> Scalar:
    """Return the subject's value for a field address.

    Args:
        field: ``"label_value"`` or ``"feature_set.<name>"``.
        subject: The predicate subject.

    Returns:
        The addressed value.

    Raises:
        PredicateError: The address names a feature the subject lacks.
    """
    return _resolve(field, subject)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve(field: str, subject: PredicateSubject) -> Scalar:
    if field == LABEL_FIELD:
        return subject.label_value
    key = field.removeprefix(FEATURE_PREFIX)
    try:
        return subject.feature_set[key]
    except KeyError:
        raise PredicateError(f"feature_set has no key {key!r}") from None


def _equal(actual: Scalar, expected: Scalar) -> bool:
    """Equality that keeps booleans, numbers, and strings apart."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return (
            isinstance(actual, bool)
            and isinstance(expected, bool)
            and (actual == expected)
        )
    if _is_number(actual) and _is_number(expected):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _compare(node: Compare, actual: Scalar) -> bool:
    op, expected = node.op, node.value
    if isinstance(expected, list):
        found = any(_equal(actual, candidate) for candidate in expected)
        return found if op == "in" else not found
    if op == "eq":
        return _equal(actual, expected)
    if op == "ne":
        return not _equal(actual, expected)
    if not _is_number(actual):
        msg = f"{node.field} is {actual!r}; op {op!r} needs a number"
        raise PredicateError(msg)
    # Both operands are numbers here: the model validator guarantees a
    # numeric ``expected`` for ordering operators.
    left, right = float(actual), float(expected)
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right
