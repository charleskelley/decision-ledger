"""The assembler: the bypass rule picks the mode; every row names the rule set.

A row within tolerance (its drift below 1% either way) is recorded ALLOW,
citing ``within-tolerance`` pinned to its version; any other row is an
exception, gated with no proposal and no citation, for the rules gate to
decide. The registration passed picks the mode. Every submission, recorded
or gated, addresses the rules gate (bound mandatory) with the rule set it is
decided under, and only it (the binding carries no configuration), names
the account as its native subject, takes the reconciliation's time as its
timestamp, and carries the row verbatim and the computation's evidence:
the drift as a numerical label, every value the computation read and gave,
and the arithmetic as the attribution.
"""

from __future__ import annotations

import pytest

from core.actions import DecisionAction
from core.gate import Compare
from core.identity import NativeSubject
from core.observation import GateContext, LabelType
from core.submission import BypassCitation, Submission, validate_submission
from mock_daily_reasoner.assembler import build_submission
from mock_daily_reasoner.compute import COMPUTE_VERSION, compute_drift
from mock_daily_reasoner.registry import (
    MOCK_DAILY_ACCOUNT_NAMESPACE,
    MOCK_DAILY_REGISTRATION,
    WITHIN_TOLERANCE_RULE,
)

#: The rules gate's context every submission carries, records included.
RULES_CONTEXT = GateContext(
    gate_id="rules", gate_config={"ruleset_id": "mock-daily-v1"}
)
#: Rows within tolerance: (target, actual).
WITHIN = {
    "settled-exactly": (1_500.0, 1_500.0),
    "0.4pct-short": (2_500.0, 2_490.0),
    "0.99pct-over": (10_000.0, 10_099.0),
}
#: Exceptions: (target, actual).
EXCEPTIONS = {
    "1pct-short": (10_000.0, 9_900.0),
    "8pct-short": (10_000.0, 9_200.0),
    "60pct-over": (1_000.0, 1_600.0),
}


def _assembled(settlement, amounts, **kwargs):
    """A row with ``amounts`` (target, actual), its drift, and its submission."""
    target, actual = amounts
    row = settlement(1, target=target, actual=actual)
    drift = compute_drift(row)
    return row, drift, build_submission(row, drift, **kwargs)


@pytest.mark.parametrize(
    "amounts",
    [*WITHIN.values(), *EXCEPTIONS.values()],
    ids=[*WITHIN, *EXCEPTIONS],
)
def test_every_submission_addresses_the_rules_gate_with_the_rule_set(
    settlement, amounts
):
    _, _, submission = _assembled(settlement, amounts)

    assert submission.gate_contexts == (RULES_CONTEXT,)
    assert submission.gate_context("rules") == RULES_CONTEXT


@pytest.mark.parametrize("amounts", list(WITHIN.values()), ids=list(WITHIN))
def test_a_row_within_tolerance_is_recorded_allow_citing_the_pinned_rule(
    settlement, amounts
):
    _, _, submission = _assembled(settlement, amounts)

    assert submission.mode == "record"
    assert submission.proposed_action == DecisionAction.ALLOW
    assert submission.bypass == BypassCitation(
        rule_id="within-tolerance", rule_version="1"
    )
    cited = validate_submission(submission, MOCK_DAILY_REGISTRATION)
    assert cited == WITHIN_TOLERANCE_RULE


@pytest.mark.parametrize("amounts", list(EXCEPTIONS.values()), ids=list(EXCEPTIONS))
def test_any_other_row_is_an_exception_gated_with_no_proposal_and_no_citation(
    settlement, amounts
):
    _, _, submission = _assembled(settlement, amounts)

    assert submission.mode == "gate"
    assert submission.proposed_action is None
    assert submission.bypass is None
    assert validate_submission(submission, MOCK_DAILY_REGISTRATION) is None


def test_the_registration_passed_picks_the_mode(settlement):
    wider = WITHIN_TOLERANCE_RULE.model_copy(
        update={
            "version": "2",
            "predicate": Compare(field="feature_set.abs_drift_pct", op="lt", value=2.0),
        }
    )
    registration = MOCK_DAILY_REGISTRATION.model_copy(update={"bypass_rules": (wider,)})
    amounts = (10_000.0, 9_850.0)  # 1.5% short

    _, _, under_its_own = _assembled(settlement, amounts)
    _, _, under_wider = _assembled(settlement, amounts, registration=registration)

    assert under_its_own.mode == "gate"
    assert under_wider.mode == "record"
    assert under_wider.bypass == BypassCitation(
        rule_id="within-tolerance", rule_version="2"
    )


@pytest.mark.parametrize(
    "amounts",
    [WITHIN["0.4pct-short"], EXCEPTIONS["8pct-short"]],
    ids=["recorded", "exception"],
)
def test_a_submission_names_the_account_and_carries_the_row_and_its_evidence(
    settlement, as_of, amounts
):
    row, drift, submission = _assembled(settlement, amounts)

    assert submission.reasoner_id == MOCK_DAILY_REGISTRATION.reasoner_id
    assert submission.event_id == row.event_id
    assert submission.timestamp == row.as_of == as_of
    assert submission.entity_id == row.entity_id
    assert submission.entity_id == MOCK_DAILY_ACCOUNT_NAMESPACE.entity_id_of(
        row.account_id
    )
    assert submission.entity_type == "account"
    assert submission.native_subject == NativeSubject(
        native_namespace="mock-daily-account", native_subject_id=row.account_id
    )
    assert submission.raw_event == row.payload()
    context = submission.reasoner_context
    assert (context.reasoner_id, context.reasoner_name) == (
        MOCK_DAILY_REGISTRATION.reasoner_id,
        MOCK_DAILY_REGISTRATION.reasoner_name,
    )
    assert (context.label_type, context.label_name, context.label_value) == (
        LabelType.NUMERICAL,
        "drift_pct",
        drift.drift_pct,
    )
    assert context.model_version == COMPUTE_VERSION == "settlement-drift-v1"
    # Complete: every value the computation read and gave, so replay and the
    # bypass rule need nothing else.
    assert context.feature_set == drift.feature_set
    assert set(context.feature_set) == {
        "target_amount",
        "actual_amount",
        "drift_amount",
        "drift_pct",
        "abs_drift_pct",
    }
    assert context.attribution is not None
    assert context.attribution.narrative == drift.narrative
    assert context.attribution.feature_contributions is None
    assert Submission.model_validate_json(submission.model_dump_json()) == submission
