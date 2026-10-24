"""The assembler: the registration's bands pick the mode; every row names the rule set.

A row a bypass rule covers is recorded with that rule's action, citing the
rule pinned to its version; a row no rule covers (one flagged for review)
is an exception, gated with no proposal and no citation. Every submission,
recorded or gated, addresses the rules gate with the rule set it is decided
under (the binding carries no configuration), names the account as its
native subject, takes the position's time as its timestamp, and carries the
row verbatim and the complete feature snapshot the scorer read. The ledger
accepts both kinds at intake.
"""

from __future__ import annotations

import pytest

from core.actions import DecisionAction
from core.identity import NativeSubject
from core.observation import GateContext, LabelType, registration_version
from core.submission import BypassCitation, Submission, validate_submission
from mock_batch_reasoner.assembler import build_submission
from mock_batch_reasoner.registry import (
    MOCK_BATCH_ACCOUNT_NAMESPACE,
    MOCK_BATCH_REGISTRATION,
)
from mock_batch_reasoner.scorer import SCORER_VERSION, score_row

#: The rules gate's context every submission carries, records included.
RULES_CONTEXT = GateContext(
    gate_id="rules", gate_config={"ruleset_id": "mock-batch-v1"}
)
#: A position in each band: its row fields, the action it is recorded with,
#: and the bypass rule it cites.
BANDS = {
    "current": ({"balance": 250.0}, DecisionAction.ALLOW, "current-account"),
    "watch": (
        {"balance": 850.0, "days_past_due": 30},
        DecisionAction.CHALLENGE,
        "watch-list",
    ),
    "delinquent": (
        {"balance": 950.0, "days_past_due": 60},
        DecisionAction.HOLD,
        "delinquent-hold",
    ),
    "charge_off": (
        {"balance": 1_200.0, "days_past_due": 90, "returned_payments": 2},
        DecisionAction.BLOCK,
        "charge-off",
    ),
}


def _assembled(row, band, *, flagged=False):
    """A row in ``band``, its score, and its submission."""
    position, _, _ = BANDS[band]
    account_day = row(1, needs_review=flagged, **position)
    score = score_row(account_day)
    return account_day, score, build_submission(account_day, score)


@pytest.mark.parametrize("flagged", [False, True], ids=["recorded", "exception"])
@pytest.mark.parametrize("band", sorted(BANDS))
def test_every_submission_addresses_the_rules_gate_with_the_rule_set(
    row, band, flagged
):
    _, _, submission = _assembled(row, band, flagged=flagged)

    assert submission.gate_contexts == (RULES_CONTEXT,)
    assert submission.gate_context("rules") == RULES_CONTEXT


@pytest.mark.parametrize("band", sorted(BANDS))
def test_a_covered_row_is_recorded_with_its_rules_action_citing_the_pinned_rule(
    row, band
):
    _, action, rule_id = BANDS[band]

    _, _, submission = _assembled(row, band)

    assert submission.mode == "record"
    assert submission.proposed_action == action
    assert submission.bypass == BypassCitation(rule_id=rule_id, rule_version="1")
    cited = validate_submission(submission, MOCK_BATCH_REGISTRATION)
    assert cited == MOCK_BATCH_REGISTRATION.bypass_rule(rule_id)


@pytest.mark.parametrize("band", sorted(BANDS))
def test_a_flagged_row_is_an_exception_gated_with_no_proposal_and_no_citation(
    row, band
):
    _, _, submission = _assembled(row, band, flagged=True)

    assert submission.mode == "gate"
    assert submission.proposed_action is None
    assert submission.bypass is None
    assert validate_submission(submission, MOCK_BATCH_REGISTRATION) is None


@pytest.mark.parametrize("flagged", [False, True], ids=["recorded", "exception"])
def test_a_submission_names_the_account_and_carries_the_row_and_its_evidence(
    row, as_of, flagged
):
    account_day, score, submission = _assembled(row, "watch", flagged=flagged)

    assert submission.reasoner_id == MOCK_BATCH_REGISTRATION.reasoner_id
    assert submission.event_id == account_day.event_id
    assert submission.timestamp == account_day.as_of == as_of
    assert submission.entity_id == account_day.entity_id
    assert submission.entity_id == MOCK_BATCH_ACCOUNT_NAMESPACE.entity_id_of(
        account_day.account_id
    )
    assert submission.entity_type == "account"
    assert submission.native_subject == NativeSubject(
        native_namespace="mock-batch-account",
        native_subject_id=account_day.account_id,
    )
    assert submission.raw_event == account_day.payload()
    context = submission.reasoner_context
    assert (context.reasoner_id, context.reasoner_name) == (
        MOCK_BATCH_REGISTRATION.reasoner_id,
        MOCK_BATCH_REGISTRATION.reasoner_name,
    )
    assert (context.label_type, context.label_name, context.label_value) == (
        LabelType.NUMERICAL,
        "delinquency_score",
        score.delinquency_score,
    )
    assert context.model_version == SCORER_VERSION
    assert context.feature_set == score.feature_set
    assert len(context.feature_set) == 6
    assert context.attribution is not None
    assert context.attribution.feature_contributions == score.contributions
    assert Submission.model_validate_json(submission.model_dump_json()) == submission


async def test_the_ledger_accepts_a_record_and_an_exception_at_intake(client, row):
    version = registration_version(MOCK_BATCH_REGISTRATION)
    _, _, recorded = _assembled(row, "current")
    _, _, exception = _assembled(row, "current", flagged=True)

    record_receipt = await client.submit(recorded)
    gate_receipt = await client.submit(exception)

    assert (record_receipt.mode, record_receipt.decision_action) == (
        "record",
        DecisionAction.ALLOW,
    )
    assert (record_receipt.bypass_rule_id, record_receipt.bypass_rule_holds) == (
        "current-account",
        True,
    )
    # The rules gate is advisory: a recorded row never reaches it.
    assert [(g.gate_id, g.status) for g in record_receipt.gates] == [
        ("rules", "skipped")
    ]
    assert gate_receipt.mode == "gate"
    assert gate_receipt.bypass_rule_holds is None
    assert [(g.gate_id, g.status) for g in gate_receipt.gates] == [("rules", "ran")]
    assert record_receipt.registration_version == version
    assert gate_receipt.registration_version == version
