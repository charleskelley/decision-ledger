"""Behavioral tests for the wire ``Submission`` and its pure intake helpers.

Contracts (DR-30, DR-31):

- Shape (model validators): a record carries a proposed action and a bypass
  citation; a gate request carries no citation and an optional proposal; the
  evidence belongs to the submitter; each gate is addressed once; unknown
  fields (the old observation's route and rationale included) are rejected;
  timestamps are timezone-aware; ``native_subject`` is optional and is the
  identity contract's ``NativeSubject`` (DR-32).
- Wire: a submission parses from JSON text in strict mode and round-trips
  unchanged; ``raw_event`` is stored verbatim; a value the ledger cannot
  store (a non-finite number, a NUL, invalid Unicode) is rejected anywhere,
  the JSON-valued fields included.
- ``effective_gate_set``: registration bindings, deployment mandates layered
  on top.
- ``validate_submission``: every registration-level rule, each with its
  stable error code — a native subject in a namespace the registration
  declares must carry the ``entity_id`` and ``entity_type`` it derives.
- ``matching_bypass_rule`` / ``evaluate_bypass``: the pure bypass helpers a
  reasoner and the ledger share.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.exceptions import SubmissionContractError
from core.gate import Compare
from core.identity import NativeNamespace
from core.observation import BypassRule, GateBinding, GateContext, ReasonerRegistration
from core.submission import (
    BypassCitation,
    NativeSubject,
    Submission,
    effective_gate_set,
    evaluate_bypass,
    evaluate_competing_rules,
    matching_bypass_rule,
    storage_problem,
    validate_submission,
)

ALLOW, CHALLENGE, HOLD, BLOCK = (
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
)


def _fields(submission: Submission, **overrides: object) -> dict[str, object]:
    """The submission's fields as Python objects, with overrides applied."""
    fields = {name: getattr(submission, name) for name in Submission.model_fields}
    fields.update(overrides)
    return fields


def _rebuild(submission: Submission, **overrides: object) -> Submission:
    """Construct a submission from another's fields (validators run)."""
    return Submission(**_fields(submission, **overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_the_baseline_fixtures_are_a_record_and_a_gate_request(
    submission, gated_submission
):
    assert submission.mode == "record"
    assert submission.proposed_action == ALLOW
    assert submission.bypass == BypassCitation(rule_id="low-score", rule_version="1")
    assert gated_submission.mode == "gate"
    assert gated_submission.proposed_action is None
    assert gated_submission.bypass is None


def test_the_schema_version_is_two(submission):
    assert submission.schema_version == 2
    with pytest.raises(ValidationError):
        _rebuild(submission, schema_version=1)


def test_a_record_needs_a_bypass_citation(submission):
    with pytest.raises(ValidationError, match="needs a bypass citation"):
        _rebuild(submission, bypass=None)


def test_a_record_needs_a_proposed_action(submission):
    with pytest.raises(ValidationError, match="needs proposed_action"):
        _rebuild(submission, proposed_action=None)


def test_a_gate_request_takes_no_bypass_citation(gated_submission):
    with pytest.raises(ValidationError, match="takes no bypass citation"):
        _rebuild(gated_submission, bypass=BypassCitation(rule_id="low-score"))


def test_a_gate_request_may_propose_an_action(gated_submission):
    proposed = _rebuild(gated_submission, proposed_action=CHALLENGE)
    assert proposed.proposed_action == CHALLENGE


def test_an_unknown_mode_is_rejected(submission):
    with pytest.raises(ValidationError):
        _rebuild(submission, mode="fast_path")


def test_the_evidence_must_belong_to_the_submitter(submission, reasoner_context):
    other = reasoner_context.model_copy(update={"reasoner_id": "other-reasoner"})
    with pytest.raises(ValidationError, match="does not match reasoner_id"):
        _rebuild(submission, reasoner_context=other)


def test_each_gate_is_addressed_at_most_once(submission):
    twice = (GateContext(gate_id="fake"), GateContext(gate_id="fake"))
    with pytest.raises(ValidationError, match="more than one gate context"):
        _rebuild(submission, gate_contexts=twice)


def test_identity_strings_must_not_be_empty(submission):
    for field in ("reasoner_id", "event_id", "entity_type"):
        with pytest.raises(ValidationError):
            _rebuild(submission, **{field: ""})


def test_the_timestamp_must_be_timezone_aware(submission):
    with pytest.raises(ValidationError):
        _rebuild(submission, timestamp=datetime(2024, 1, 15, 10, 30))


@pytest.mark.parametrize(
    "legacy_field",
    ["route", "fast_path_rationale", "gate_context"],
)
def test_the_old_observation_fields_are_not_part_of_the_wire_schema(
    submission, legacy_field
):
    payload = json.loads(submission.model_dump_json())
    payload[legacy_field] = "FAST_PATH_ALLOW"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Submission.model_validate_json(json.dumps(payload))


def test_raw_event_and_reasoner_context_are_required(submission):
    for field in ("raw_event", "reasoner_context"):
        fields = _fields(submission)
        del fields[field]
        with pytest.raises(ValidationError):
            Submission(**fields)  # type: ignore[arg-type]


def test_a_submission_may_name_its_native_subject(submission):
    subject = NativeSubject(native_namespace="widget-db", native_subject_id="w-1")
    named = _rebuild(submission, native_subject=subject)
    assert named.native_subject == subject
    assert submission.native_subject is None
    with pytest.raises(ValidationError):
        NativeSubject(native_namespace="", native_subject_id="w-1")


def test_the_native_subject_is_the_identity_contracts():
    """``core.submission`` re-exports the one ``NativeSubject`` (DR-32)."""
    import core.identity

    assert NativeSubject is core.identity.NativeSubject


def test_a_submission_is_immutable(submission):
    with pytest.raises(ValidationError):
        submission.mode = "gate"


def test_gate_context_returns_the_envelope_for_a_gate(submission, gate_context):
    assert submission.gate_context("fake") == gate_context
    assert submission.gate_context("other") is None


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def test_a_submission_parses_from_wire_json_in_strict_mode(submission):
    wire = {
        "schema_version": 2,
        "reasoner_id": "stub-reasoner",
        "event_id": "evt-wire-1",
        "timestamp": "2024-01-15T10:30:00Z",
        "entity_id": "2c1b5a9e-4a43-5b9e-9a3c-7c1d2e3f4a5b",
        "entity_type": "widget",
        "mode": "record",
        "proposed_action": "ALLOW",
        "bypass": {"rule_id": "low-score"},
        "reasoner_context": json.loads(submission.reasoner_context.model_dump_json()),
        "gate_contexts": [{"gate_id": "fake", "gate_config": {"confidence": 0.9}}],
        "raw_event": {"widget_id": "w-1", "tags": ["a", None, 3], "nested": {"x": 1.5}},
    }
    parsed = Submission.model_validate_json(json.dumps(wire))
    assert parsed.entity_id == uuid.UUID(wire["entity_id"])
    assert parsed.timestamp == datetime(2024, 1, 15, 10, 30, tzinfo=UTC)
    assert parsed.proposed_action == ALLOW
    assert parsed.gate_contexts == (
        GateContext(gate_id="fake", gate_config={"confidence": 0.9}),
    )


def test_a_submission_round_trips_through_json_unchanged(submission):
    assert Submission.model_validate_json(submission.model_dump_json()) == submission


def test_raw_event_is_stored_verbatim(submission):
    event = {"a": [1, "two", None, {"b": False}], "c": {"d": 2.5}}
    carried = _rebuild(submission, raw_event=event)
    assert json.loads(carried.model_dump_json())["raw_event"] == event


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("raw_event", {"lat": float("nan")}, "a non-finite number (nan)"),
        ("raw_event", {"scores": [1.0, float("inf")]}, "a non-finite number (inf)"),
        (
            "gate_contexts",
            [{"gate_id": "fake", "gate_config": {"confidence": float("-inf")}}],
            "a non-finite number (-inf)",
        ),
        ("raw_event", {"ua": "Mozilla\x00"}, "a NUL character"),
        ("raw_event", {"key\x00": 1}, "a NUL character"),
    ],
)
def test_wire_json_holding_a_value_the_ledger_cannot_store_is_rejected(
    submission, field, value, problem
):
    # json.dumps writes NaN / Infinity literals and \u0000 escapes: JSON input
    # reaches the JSON-valued fields with values a Python caller could not
    # pass, and none of them would survive the bundle's round trip.
    wire = json.loads(submission.model_dump_json())
    wire[field] = value
    with pytest.raises(ValidationError, match=re.escape(problem)):
        Submission.model_validate_json(json.dumps(wire))


def test_a_string_that_is_not_valid_unicode_is_rejected(submission):
    with pytest.raises(ValidationError, match="not valid Unicode"):
        _rebuild(submission, raw_event={"ua": "Mozilla\ud800"})


def test_storage_problem_accepts_json_and_names_the_first_unstorable_value():
    assert storage_problem({"a": [1, True, None, 2.5, "x", {"b": "y"}]}) is None
    assert storage_problem({"a": {"b\x00": 1}}) == "a NUL character"
    assert storage_problem([1, [2, float("nan")]]) == "a non-finite number (nan)"
    assert storage_problem(("ok", "\ud800")) == (
        "a string that is not valid Unicode (a lone surrogate)"
    )


# ---------------------------------------------------------------------------
# effective_gate_set
# ---------------------------------------------------------------------------


def _registration(**overrides: object) -> ReasonerRegistration:
    fields: dict[str, object] = {
        "reasoner_id": "stub-reasoner",
        "reasoner_name": "Stub",
        "gates": (GateBinding(gate_id="fake"),),
        "bypass_rules": (
            BypassRule(
                rule_id="low-score",
                version="1",
                description="",
                predicate=Compare(field="label_value", op="lt", value=0.2),
                action=ALLOW,
            ),
        ),
        "recordable_actions": frozenset({ALLOW, BLOCK}),
    }
    fields.update(overrides)
    return ReasonerRegistration(**fields)  # type: ignore[arg-type]


def test_the_gate_set_is_the_bindings_in_order_without_deployment_mandates():
    reg = _registration(
        gates=(GateBinding(gate_id="b"), GateBinding(gate_id="a", mandatory=True))
    )
    assert effective_gate_set(reg) == reg.gates


def test_a_deployment_mandate_upgrades_an_advisory_binding():
    reg = _registration(gates=(GateBinding(gate_id="fake"),))
    assert effective_gate_set(reg, {"fake"}) == (
        GateBinding(gate_id="fake", mandatory=True),
    )


def test_deployment_gates_the_reasoner_does_not_bind_join_as_mandatory_sorted():
    reg = _registration(gates=(GateBinding(gate_id="fake"),))
    assert effective_gate_set(reg, ["zeta", "alpha"]) == (
        GateBinding(gate_id="fake"),
        GateBinding(gate_id="alpha", mandatory=True),
        GateBinding(gate_id="zeta", mandatory=True),
    )


def test_an_empty_registration_and_no_mandates_is_an_empty_gate_set():
    assert effective_gate_set(_registration(gates=())) == ()


# ---------------------------------------------------------------------------
# validate_submission
# ---------------------------------------------------------------------------


def _code(err: pytest.ExceptionInfo[SubmissionContractError]) -> str:
    return err.value.code


def test_a_valid_record_returns_the_cited_rule(submission):
    rule = validate_submission(submission, _registration())
    assert rule is not None
    assert rule.rule_id == "low-score"


def test_a_valid_gate_request_returns_no_rule(gated_submission):
    assert validate_submission(gated_submission, _registration()) is None


def test_a_registration_for_another_reasoner_is_refused(submission):
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(submission, _registration(reasoner_id="other"))
    assert _code(err) == "SUBMISSION_INVALID"


def test_citing_an_unregistered_rule_is_refused(submission):
    cited = _rebuild(submission, bypass=BypassCitation(rule_id="made-up"))
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(cited, _registration())
    assert _code(err) == "BYPASS_RULE_UNREGISTERED"
    assert "made-up" in err.value.violation
    assert err.value.reasoner_id == "stub-reasoner"


def test_recording_an_action_the_reasoner_may_not_record_is_refused(submission):
    hold = _rebuild(submission, proposed_action=HOLD)
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(hold, _registration())
    assert _code(err) == "ACTION_NOT_RECORDABLE"


def test_a_rule_justifies_only_its_own_action(submission):
    # block-band-style abuse: the cited rule holds but justifies ALLOW, and
    # the record claims BLOCK — or the reverse. Either way it is refused.
    block = _rebuild(submission, proposed_action=BLOCK)
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(block, _registration())
    assert _code(err) == "BYPASS_ACTION_MISMATCH"


def test_a_version_pin_must_match_the_registered_rule(submission):
    stale = _rebuild(
        submission, bypass=BypassCitation(rule_id="low-score", rule_version="0")
    )
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(stale, _registration())
    assert _code(err) == "BYPASS_RULE_VERSION_MISMATCH"


def test_an_unpinned_citation_is_accepted(submission):
    unpinned = _rebuild(submission, bypass=BypassCitation(rule_id="low-score"))
    assert validate_submission(unpinned, _registration()) is not None


def test_a_context_for_a_gate_outside_the_gate_set_is_refused(submission):
    extra = _rebuild(
        submission,
        gate_contexts=(GateContext(gate_id="fake"), GateContext(gate_id="rogue")),
    )
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(extra, _registration())
    assert _code(err) == "GATE_NOT_BOUND"
    assert "rogue" in err.value.violation


def test_a_context_for_an_unbound_deployment_mandated_gate_is_refused(submission):
    # The deployment mandated the gate; a reasoner that does not bind it
    # cannot steer it (which list or rule set it checks).
    extra = _rebuild(
        submission,
        gate_contexts=(GateContext(gate_id="fake"), GateContext(gate_id="lookup")),
    )
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(extra, _registration(), deployment_mandatory={"lookup"})
    assert _code(err) == "GATE_NOT_BOUND"


def test_a_bound_gate_the_deployment_also_mandates_takes_its_context(submission):
    bound = _registration(
        gates=(GateBinding(gate_id="fake"), GateBinding(gate_id="lookup"))
    )
    both = _rebuild(
        submission,
        gate_contexts=(GateContext(gate_id="fake"), GateContext(gate_id="lookup")),
    )
    assert validate_submission(both, bound, deployment_mandatory={"lookup"}) is not None


@pytest.mark.parametrize("fixture", ["submission", "gated_submission"])
def test_every_bound_gate_needs_a_context_in_both_modes(request, fixture):
    base = request.getfixturevalue(fixture)
    bare = _rebuild(base, gate_contexts=())
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(bare, _registration())
    assert _code(err) == "GATE_CONTEXT_MISSING"


def test_a_deployment_mandated_gate_may_go_without_a_context(submission):
    assert (
        validate_submission(submission, _registration(), deployment_mandatory={"lk"})
        is not None
    )


def test_a_gate_request_needs_at_least_one_gate(gated_submission):
    lone = _rebuild(gated_submission, gate_contexts=())
    with pytest.raises(SubmissionContractError) as err:
        validate_submission(lone, _registration(gates=()))
    assert _code(err) == "NO_GATES"


def test_a_gate_request_with_only_deployment_gates_is_accepted(gated_submission):
    lone = _rebuild(gated_submission, gate_contexts=())
    assert (
        validate_submission(lone, _registration(gates=()), deployment_mandatory={"lk"})
        is None
    )


def test_a_record_needs_no_gate_at_all(submission):
    lone = _rebuild(submission, gate_contexts=())
    assert validate_submission(lone, _registration(gates=())) is not None


def test_the_error_message_names_the_reasoner_and_the_code(submission):
    cited = _rebuild(submission, bypass=BypassCitation(rule_id="nope"))
    with pytest.raises(SubmissionContractError, match=r"\[stub-reasoner\] BYPASS"):
        validate_submission(cited, _registration())


# ---------------------------------------------------------------------------
# matching_bypass_rule / evaluate_bypass
# ---------------------------------------------------------------------------


def _rule(rule_id: str, predicate: object, action: DecisionAction) -> BypassRule:
    return BypassRule(
        rule_id=rule_id,
        version="1",
        description="",
        predicate=predicate,  # type: ignore[arg-type]
        action=action,
    )


def test_among_equally_strict_holding_rules_the_first_registered_matches(
    reasoner_context,
):
    first = _rule("first", Compare(field="label_value", op="lt", value=0.5), ALLOW)
    second = _rule("second", Compare(field="label_value", op="lt", value=0.9), ALLOW)
    reg = _registration(bypass_rules=(first, second))
    assert matching_bypass_rule(reg, reasoner_context) == first


def test_the_strictest_holding_rule_matches_whatever_its_position(reasoner_context):
    lenient = _rule("lenient", Compare(field="label_value", op="lt", value=0.5), ALLOW)
    strict = _rule("strict", Compare(field="label_value", op="gt", value=0.1), BLOCK)
    reg = _registration(bypass_rules=(lenient, strict))
    assert matching_bypass_rule(reg, reasoner_context) == strict


def test_competing_rules_are_every_other_registered_rule(reasoner_context):
    low = _rule("low", Compare(field="label_value", op="lt", value=0.2), ALLOW)
    high = _rule("high", Compare(field="label_value", op="gt", value=0.9), BLOCK)
    mid = _rule("mid", Compare(field="label_value", op="gt", value=0.1), BLOCK)
    reg = _registration(bypass_rules=(low, high, mid))
    checks = evaluate_competing_rules(reg, low, reasoner_context)
    assert [c.rule.rule_id for c in checks] == ["high", "mid"]
    assert [c.holds for c in checks] == [False, True]


def test_no_holding_rule_means_no_match(reasoner_context):
    never = _rule("never", Compare(field="label_value", op="gt", value=0.99), BLOCK)
    assert (
        matching_bypass_rule(_registration(bypass_rules=(never,)), reasoner_context)
        is None
    )


def test_a_rule_that_cannot_evaluate_does_not_match_and_the_next_one_is_tried(
    reasoner_context,
):
    broken = _rule(
        "broken", Compare(field="feature_set.absent", op="eq", value=1), ALLOW
    )
    fine = _rule("fine", Compare(field="label_value", op="lt", value=0.2), ALLOW)
    reg = _registration(bypass_rules=(broken, fine))
    assert matching_bypass_rule(reg, reasoner_context) == fine


def test_evaluate_bypass_records_the_rule_and_the_values_it_read(reasoner_context):
    rule = _rule("low", Compare(field="label_value", op="lt", value=0.2), ALLOW)
    check = evaluate_bypass(rule, reasoner_context)
    assert check.holds is True
    assert check.rule == rule
    assert check.observed == {"label_value": 0.15}
    assert check.error is None


def test_evaluate_bypass_reports_a_rule_that_does_not_hold(reasoner_context):
    rule = _rule("high", Compare(field="label_value", op="gt", value=0.9), BLOCK)
    check = evaluate_bypass(rule, reasoner_context)
    assert check.holds is False
    assert check.error is None


def test_evaluate_bypass_fails_closed_when_the_predicate_cannot_evaluate(
    reasoner_context,
):
    rule = _rule("absent", Compare(field="feature_set.absent", op="eq", value=1), ALLOW)
    check = evaluate_bypass(rule, reasoner_context)
    assert check.holds is False
    assert "absent" in (check.error or "")
    assert check.observed == {}


def test_evaluate_bypass_is_deterministic_and_round_trips(reasoner_context):
    rule = _rule("low", Compare(field="label_value", op="lt", value=0.2), ALLOW)
    first, second = (
        evaluate_bypass(rule, reasoner_context),
        evaluate_bypass(rule, reasoner_context),
    )
    assert first == second
    assert type(first).model_validate_json(first.model_dump_json()) == first


# ---------------------------------------------------------------------------
# Native subjects in declared namespaces (DR-32)
# ---------------------------------------------------------------------------

_WIDGET_DB = NativeNamespace(
    native_namespace="widget-db",
    namespace_uuid=uuid.UUID("3f6e1b7c-9d2a-5e48-8b1f-0c7a4d9e2b61"),
    entity_type="widget",
)


def _declaring(stub_registration):
    fields = {
        name: getattr(stub_registration, name)
        for name in ReasonerRegistration.model_fields
    }
    return ReasonerRegistration(**{**fields, "native_namespaces": (_WIDGET_DB,)})


def _named(submission, key: str = "w-1", **overrides):
    native = NativeSubject(native_namespace="widget-db", native_subject_id=key)
    fields = {
        "native_subject": native,
        "entity_id": _WIDGET_DB.entity_id_of(key),
        "entity_type": "widget",
    }
    fields.update(overrides)
    return _rebuild(submission, **fields)


def test_a_native_subject_in_a_declared_namespace_matches_its_entity(
    submission, stub_registration
):
    assert validate_submission(_named(submission), _declaring(stub_registration))


@pytest.mark.parametrize(
    "overrides",
    [
        {"entity_id": uuid.uuid4()},
        {"entity_type": "gadget"},
        {"entity_id": uuid.uuid5(_WIDGET_DB.namespace_uuid, "w-2")},
    ],
    ids=["another-id", "another-type", "another-key"],
)
def test_a_native_subject_that_contradicts_its_declared_namespace_is_refused(
    submission, stub_registration, overrides
):
    with pytest.raises(SubmissionContractError) as caught:
        validate_submission(
            _named(submission, **overrides), _declaring(stub_registration)
        )
    assert caught.value.code == "NATIVE_SUBJECT_MISMATCH"
    assert "w-1" not in caught.value.violation  # the native key is not echoed


def test_a_native_subject_in_an_undeclared_namespace_is_not_checked(
    submission, stub_registration
):
    named = _named(submission, entity_id=uuid.uuid4())
    assert validate_submission(named, stub_registration)  # declares nothing
    elsewhere = _rebuild(
        named,
        native_subject=NativeSubject(
            native_namespace="other-db", native_subject_id="k"
        ),
    )
    assert validate_submission(elsewhere, _declaring(stub_registration))
