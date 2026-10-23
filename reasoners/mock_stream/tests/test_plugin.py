"""The stream mock's plugin: what a deployment that installs it discovers.

``PLUGIN`` publishes the registration and the prompts and corpus the RAG
policy gate reads; it is installed under both entry-point groups by its
reasoner id, and its registration binds the policy gate (advisory) to the
mock's own corpus at tier 2, records one band per recordable action, names
its subjects, and holds a fresh account for review (Tier 3).
"""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

from app.plugin import ReasonerPlugin
from app.plugins import PLUGIN_GROUP, discover_plugins
from core.actions import DecisionAction
from core.gate import Compare
from eval.adapter import EVAL_ADAPTER_GROUP, load_eval_adapter
from mock_stream_reasoner.eval.adapter import ADAPTER
from mock_stream_reasoner.events import TransferEvent
from mock_stream_reasoner.plugin import ASSETS_DIR, PLUGIN
from mock_stream_reasoner.registry import (
    MOCK_STREAM_ACCOUNT_NAMESPACE,
    MOCK_STREAM_CORPUS_ID,
    MOCK_STREAM_REGISTRATION,
    STEADY_THRESHOLD,
    SURGE_THRESHOLD,
)

REASONER_ID = "mock-stream-reasoner"
#: The member's directory (``reasoners/mock_stream``).
MEMBER_DIR = Path(__file__).resolve().parent.parent


def _entry_point(group):
    [match] = [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if ep.name == REASONER_ID
    ]
    return match


def test_the_plugin_publishes_the_registration_and_the_policy_gate_assets():
    assert isinstance(PLUGIN, ReasonerPlugin)
    assert PLUGIN.registration is MOCK_STREAM_REGISTRATION
    assert PLUGIN.registration.reasoner_id == REASONER_ID
    assert (PLUGIN.prompts_dir, PLUGIN.corpus_dir) == (
        ASSETS_DIR / "prompts",
        ASSETS_DIR / "corpus",
    )
    assert sorted(p.name for p in PLUGIN.prompts_dir.glob("*.yaml")) == [
        "mock-stream-v1.yaml"
    ]
    assert sorted(p.name for p in PLUGIN.corpus_dir.rglob("*.md")) == [
        "new-account-transfer-practice.md",
        "transfer-velocity-practice.md",
    ]


def test_the_installed_entry_points_resolve_to_the_singletons():
    assert _entry_point(PLUGIN_GROUP).load() is PLUGIN
    assert _entry_point(EVAL_ADAPTER_GROUP).load() is ADAPTER
    assert load_eval_adapter(REASONER_ID) is ADAPTER
    assert (ADAPTER.reasoner_id, ADAPTER.event_type) == (REASONER_ID, TransferEvent)


def test_discovery_finds_the_mock_by_its_reasoner_id():
    [plugin] = discover_plugins(allow=frozenset({REASONER_ID}))

    assert plugin is PLUGIN


def test_the_registration_binds_the_policy_gate_advisory_to_its_own_tier_2_corpus():
    registration = MOCK_STREAM_REGISTRATION

    [binding] = registration.gates
    assert (binding.gate_id, binding.mandatory) == ("policy", False)
    assert binding.config == {
        "corpora": [
            {
                "corpus_id": "mock-stream-practices",
                "provider": "reference",
                "tier": 2,
                "min_provenance": "pinned_version",
            }
        ],
        "k_per_corpus": 5,
    }
    assert MOCK_STREAM_CORPUS_ID == "mock-stream-practices"


def test_the_registration_records_one_band_per_recordable_action():
    registration = MOCK_STREAM_REGISTRATION

    assert registration.recordable_actions == frozenset(
        {DecisionAction.ALLOW, DecisionAction.BLOCK}
    )
    assert sorted(rule.action for rule in registration.bypass_rules) == sorted(
        registration.recordable_actions
    )
    steady = registration.bypass_rule("steady-activity")
    surge = registration.bypass_rule("activity-surge")
    assert steady is not None
    assert surge is not None
    assert (steady.action, steady.version, steady.predicate) == (
        DecisionAction.ALLOW,
        "1",
        Compare(field="label_value", op="lt", value=STEADY_THRESHOLD),
    )
    assert (surge.action, surge.version, surge.predicate) == (
        DecisionAction.BLOCK,
        "1",
        Compare(field="label_value", op="gt", value=SURGE_THRESHOLD),
    )
    assert STEADY_THRESHOLD < SURGE_THRESHOLD  # the gate decides the middle


def test_the_registration_names_its_subjects_and_holds_fresh_accounts():
    registration = MOCK_STREAM_REGISTRATION

    assert registration.native_namespaces == (MOCK_STREAM_ACCOUNT_NAMESPACE,)
    namespace = registration.native_namespace("mock-stream-account")
    assert namespace is not None
    assert namespace.entity_type == "account"
    assert registration.novel_entity_feature == "thin_history"


def test_prepare_reasoners_loads_the_corpus_under_the_id_the_binding_names():
    makefile = (MEMBER_DIR / "reasoner.mk").read_text(encoding="utf-8")
    match = re.search(
        rf"^POLICY_CORPUS_ID_{REASONER_ID}\s*:=\s*(\S+)\s*$", makefile, re.MULTILINE
    )

    assert match is not None
    [binding] = MOCK_STREAM_REGISTRATION.gates
    corpora = binding.config["corpora"]
    assert isinstance(corpora, list)
    assert [corpus["corpus_id"] for corpus in corpora] == [match.group(1)]
