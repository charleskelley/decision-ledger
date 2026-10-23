"""The assembler: the registration's bands pick the mode; every submission gates.

A score inside a band is recorded with the band's action, citing its bypass
rule pinned to the rule's version; any other score asks the ledger to gate
the transfer, with no proposal. Every submission, recorded or gated,
addresses the policy gate with the template the mock ships, its variables
as text, the GLOBAL jurisdiction, and a retrieval query; names the account as
its native subject; and carries the transfer verbatim and the complete
feature snapshot the scorer read.
"""

from __future__ import annotations

import re

import pytest
import yaml

from core.actions import DecisionAction
from core.identity import NativeSubject
from core.observation import LabelType
from core.submission import BypassCitation, Submission, validate_submission
from mock_stream_reasoner.assembler import (
    GLOBAL_JURISDICTION,
    TEMPLATE_ID,
    build_submission,
)
from mock_stream_reasoner.plugin import PLUGIN
from mock_stream_reasoner.registry import (
    MOCK_STREAM_ACCOUNT_NAMESPACE,
    MOCK_STREAM_REGISTRATION,
    STEADY_THRESHOLD,
    SURGE_THRESHOLD,
)
from mock_stream_reasoner.scorer import SCORER_VERSION, score_transfer
from mock_stream_reasoner.state import AccountWindows

_ESTABLISHED = [(0.0, 100.0), (20.0, 100.0), (40.0, 100.0)]

#: Each scenario's account history ([(minutes, amount)]) and its last
#: transfer (amount, minutes), the one assembled.
_SCENARIOS = {
    "steady": (_ESTABLISHED, 100.0, 60.0),
    "surge": ([(float(minute), 100.0) for minute in range(6)], 100.0, 6.0),
    "fresh_large": ([], 500.0, 0.0),
    "established_large": (_ESTABLISHED, 600.0, 60.0),
}


def _assembled(transfer, scenario):
    """The scenario's last transfer, its score, and its submission."""
    history, amount, minutes = _SCENARIOS[scenario]
    account = f"acct-{scenario}"
    windows = AccountWindows()
    for n, (at, earlier) in enumerate(history, 1):
        windows.observe(transfer(n, account, earlier, minutes=at))
    event = transfer(len(history) + 1, account, amount, minutes=minutes)
    score = score_transfer(windows.observe(event))
    return event, score, build_submission(event, score)


def _template_placeholders():
    """The variables the shipped template renders (the gate fills the evidence)."""
    path = PLUGIN.prompts_dir / f"{TEMPLATE_ID}.yaml"
    template = yaml.safe_load(path.read_text(encoding="utf-8"))["template"]
    return set(re.findall(r"\{(\w+)\}", template)) - {"policy_snippets"}


@pytest.mark.parametrize(
    ("scenario", "action", "rule_id"),
    [
        ("steady", DecisionAction.ALLOW, "steady-activity"),
        ("surge", DecisionAction.BLOCK, "activity-surge"),
    ],
)
def test_a_score_in_a_band_is_recorded_with_its_action_citing_the_pinned_rule(
    transfer, scenario, action, rule_id
):
    _, _, submission = _assembled(transfer, scenario)

    assert submission.mode == "record"
    assert submission.proposed_action == action
    assert submission.bypass == BypassCitation(rule_id=rule_id, rule_version="1")
    cited = validate_submission(submission, MOCK_STREAM_REGISTRATION)
    assert cited == MOCK_STREAM_REGISTRATION.bypass_rule(rule_id)


@pytest.mark.parametrize("scenario", ["fresh_large", "established_large"])
def test_a_score_between_the_bands_asks_the_ledger_to_gate_with_no_proposal(
    transfer, scenario
):
    _, score, submission = _assembled(transfer, scenario)

    assert STEADY_THRESHOLD < score.activity_score < SURGE_THRESHOLD
    assert submission.mode == "gate"
    assert submission.proposed_action is None
    assert submission.bypass is None
    assert validate_submission(submission, MOCK_STREAM_REGISTRATION) is None


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_every_submission_addresses_the_policy_gate_with_the_shipped_template(
    transfer, scenario
):
    _, _, submission = _assembled(transfer, scenario)

    [context] = submission.gate_contexts
    assert context.gate_id == "policy"
    assert submission.gate_context("policy") == context
    config = context.gate_config
    assert config is not None
    assert config["template_id"] == TEMPLATE_ID == "mock-stream-v1"
    assert config["jurisdictions"] == [GLOBAL_JURISDICTION] == ["GLOBAL"]
    query = config["retrieval_query"]
    assert isinstance(query, str)
    assert query.strip()
    variables = config["template_vars"]
    assert isinstance(variables, dict)
    placeholders = _template_placeholders()
    assert placeholders
    assert placeholders <= set(variables)
    assert all(isinstance(value, str) for value in variables.values())


@pytest.mark.parametrize("scenario", ["fresh_large", "established_large"])
def test_the_template_variables_render_the_transfers_features(transfer, scenario):
    _, score, submission = _assembled(transfer, scenario)

    config = submission.gate_contexts[0].gate_config
    assert config is not None
    variables = config["template_vars"]
    assert isinstance(variables, dict)
    features = score.feature_set
    assert float(str(variables["activity_score"])) == pytest.approx(
        score.activity_score, abs=5e-4
    )
    for name in ("amount", "baseline_amount", "amount_ratio"):
        assert float(str(variables[name])) == pytest.approx(features[name], abs=5e-3)
    for name in ("window_events", "prior_events", "thin_history"):
        assert variables[name] == str(features[name])


def test_the_retrieval_query_describes_the_accounts_history(transfer):
    _, _, fresh = _assembled(transfer, "fresh_large")
    _, established_score, established = _assembled(transfer, "established_large")

    fresh_query = fresh.gate_contexts[0].gate_config["retrieval_query"]
    established_query = established.gate_contexts[0].gate_config["retrieval_query"]

    assert "new account" in fresh_query
    assert "thin" in fresh_query
    prior = established_score.features.prior_events
    assert f"{prior} prior transfers" in established_query
    assert "thin" not in established_query


@pytest.mark.parametrize("scenario", ["steady", "fresh_large"])
def test_a_submission_names_the_account_and_carries_the_transfer_and_its_evidence(
    transfer, scenario
):
    event, score, submission = _assembled(transfer, scenario)

    assert submission.reasoner_id == MOCK_STREAM_REGISTRATION.reasoner_id
    assert (submission.event_id, submission.timestamp) == (
        event.event_id,
        event.timestamp,
    )
    assert submission.native_subject == NativeSubject(
        native_namespace="mock-stream-account", native_subject_id=event.account_id
    )
    assert submission.entity_id == MOCK_STREAM_ACCOUNT_NAMESPACE.entity_id_of(
        event.account_id
    )
    assert submission.entity_type == "account"
    assert submission.raw_event == event.model_dump(mode="json")
    context = submission.reasoner_context
    assert (context.label_type, context.label_name, context.label_value) == (
        LabelType.NUMERICAL,
        "activity_score",
        score.activity_score,
    )
    assert context.model_version == SCORER_VERSION
    assert context.feature_set == score.feature_set
    assert MOCK_STREAM_REGISTRATION.novel_entity_feature in context.feature_set
    assert context.attribution is not None
    assert context.attribution.feature_contributions == score.contributions
    assert Submission.model_validate_json(submission.model_dump_json()) == submission
