"""The example's submissions: recorded in a band, gated between them.

Every submission addresses the policy gate (recorded ones too, for shadow
evaluation) with a context the gate accepts — its config model validates it
and the profile it derives from the example's own prompts and corpus
authorizes it.
"""

from __future__ import annotations

import pytest

from core.actions import DecisionAction
from core.submission import Submission
from example_reasoner.__main__ import sample_event
from example_reasoner.assembler import CORPUS_VERSION, TEMPLATE_ID, build_submission
from example_reasoner.plugin import PLUGIN
from example_reasoner.scorer import score_widget

policy_rag_gate = pytest.importorskip("policy_rag_gate")


@pytest.mark.parametrize(
    ("band", "mode", "action", "rule_id"),
    [
        ("low", "record", DecisionAction.ALLOW, "low-score"),
        ("mid", "gate", None, None),
        ("high", "record", DecisionAction.BLOCK, "high-score"),
    ],
)
@pytest.mark.parametrize("region", ["REGION_A", "REGION_B"])
def test_the_mode_follows_the_bypass_rules_and_the_gate_accepts_every_context(
    band, mode, action, rule_id, region
):
    from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile

    event = sample_event(band, region=region)
    submission = build_submission(event, score_widget(event))

    assert submission.mode == mode
    assert submission.proposed_action == action
    if rule_id is None:
        assert submission.bypass is None
    else:
        assert submission.bypass is not None
        assert (submission.bypass.rule_id, submission.bypass.rule_version) == (
            rule_id,
            "1",
        )
    [context] = submission.gate_contexts
    config = PolicyGateConfig.model_validate(context.gate_config)
    ReasonerProfile.from_assets(
        prompts_dir=PLUGIN.prompts_dir, corpus_dir=PLUGIN.corpus_dir
    ).authorize(config)
    assert config.template_id == TEMPLATE_ID
    assert config.corpus_version == CORPUS_VERSION
    assert config.jurisdictions == [region, "GLOBAL"]


def test_a_submission_round_trips_and_carries_the_event_verbatim():
    event = sample_event("mid")
    submission = build_submission(event, score_widget(event))

    assert Submission.model_validate_json(submission.model_dump_json()) == submission
    assert submission.raw_event == event.model_dump(mode="json")
    assert submission.entity_id == event.entity_id
    assert submission.entity_type == "widget"
    assert submission.reasoner_context.label_name == "widget_score"


def test_every_widget_is_one_subject():
    first = sample_event("low", widget_id="widget-7")
    second = sample_event("high", widget_id="widget-7")
    assert first.entity_id == second.entity_id
    assert first.event_id != second.event_id
