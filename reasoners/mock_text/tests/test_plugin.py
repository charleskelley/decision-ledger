"""The text mock's plugin: what a deployment that installs it discovers.

``PLUGIN`` publishes the registration, the prompt template, and the parent
of the two corpora the binding names (``regulatory/`` and ``practice/``, two
documents each); it is installed under both entry-point groups by its
reasoner id. Composed in memory with the scripted policy double, the
deployment admits the binding with both corpora, refuses to compose when the
double reports a bound corpus not loaded (as the RAG policy gate refuses a
corpus its provider does not hold), and asks the double, with the binding,
about every message it decides. Its ``reasoner.mk`` loads each corpus the
binding names with a loader call of its own, from its own subdirectory, and
joins the mock evals, never the reasoner evals.
"""

from __future__ import annotations

import importlib.metadata
import re
from typing import TYPE_CHECKING

import pytest

from app.plugin import ReasonerPlugin
from app.plugins import PLUGIN_GROUP, discover_plugins
from core.exceptions import GatePluginError
from eval.adapter import EVAL_ADAPTER_GROUP, load_eval_adapter
from mock_text_reasoner.eval.adapter import ADAPTER
from mock_text_reasoner.messages import CustomerMessage
from mock_text_reasoner.pipeline import decide_message
from mock_text_reasoner.plugin import (
    ASSETS_DIR,
    CORPUS_DIR,
    PLUGIN,
    PRACTICE_DIR,
    REGULATORY_DIR,
)
from mock_text_reasoner.registry import MOCK_TEXT_REGISTRATION, POLICY_GATE_BINDING
from mock_text_reasoner.testing import ScriptedPolicyGate, ScriptedPolicyGatePlugin
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from pathlib import Path

    from app.client import LocalClient
    from app.plugin import FrameworkServices

REASONER_ID = "mock-text-reasoner"
#: The corpora the binding names, in order, and the subdirectory of each.
CORPORA = [("mock-text-regulatory", "regulatory"), ("mock-text-practice", "practice")]


def _entry_point(group: str) -> importlib.metadata.EntryPoint:
    [match] = [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if ep.name == REASONER_ID
    ]
    return match


def test_the_plugin_publishes_the_registration_the_template_and_two_corpora():
    assert isinstance(PLUGIN, ReasonerPlugin)
    assert PLUGIN.registration is MOCK_TEXT_REGISTRATION
    assert PLUGIN.registration.reasoner_id == REASONER_ID
    assert (PLUGIN.prompts_dir, PLUGIN.corpus_dir) == (
        ASSETS_DIR / "prompts",
        ASSETS_DIR / "corpus",
    )
    assert sorted(p.name for p in PLUGIN.prompts_dir.glob("*.yaml")) == [
        "mock-text-v1.yaml"
    ]
    assert PLUGIN.corpus_dir == CORPUS_DIR
    assert (
        PLUGIN.corpus_dir / "regulatory",
        PLUGIN.corpus_dir / "practice",
    ) == (REGULATORY_DIR, PRACTICE_DIR)
    assert sorted(
        p.name for p in PLUGIN.corpus_dir.iterdir() if not p.name.startswith(".")
    ) == ["practice", "regulatory"]
    assert sorted(
        str(p.relative_to(PLUGIN.corpus_dir)) for p in PLUGIN.corpus_dir.rglob("*.md")
    ) == [
        "practice/assistant-reply-practice.md",
        "practice/escalation-practice.md",
        "regulatory/assistant-communications-rule.md",
        "regulatory/credential-and-fraud-rule.md",
    ]


def test_the_installed_entry_points_resolve_to_the_singletons():
    assert _entry_point(PLUGIN_GROUP).load() is PLUGIN
    assert _entry_point(EVAL_ADAPTER_GROUP).load() is ADAPTER
    assert load_eval_adapter(REASONER_ID) is ADAPTER
    assert (ADAPTER.reasoner_id, ADAPTER.event_type) == (REASONER_ID, CustomerMessage)


def test_discovery_finds_the_mock_by_its_reasoner_id():
    [plugin] = discover_plugins(allow=frozenset({REASONER_ID}))

    assert plugin is PLUGIN


def test_the_deployment_admits_the_binding_with_both_corpora(
    ledger: FrameworkServices, policy_gate: ScriptedPolicyGate
):
    [(reasoner_id, binding)] = policy_gate.bindings

    assert reasoner_id == REASONER_ID
    assert [
        (corpus.corpus_id, corpus.provider, corpus.tier, corpus.min_provenance)
        for corpus in binding.corpora
    ] == [
        ("mock-text-regulatory", "reference", 1, "pinned_version"),
        ("mock-text-practice", "reference", 2, "pinned_version"),
    ]
    assert binding.k_per_corpus == 5
    assert ledger.gates["policy"].gate is policy_gate
    assert ledger.registry.get(REASONER_ID) == MOCK_TEXT_REGISTRATION


@pytest.mark.parametrize(
    ("loaded", "missing"),
    [
        pytest.param(
            {"mock-text-regulatory"}, ["mock-text-practice"], id="practice-missing"
        ),
        pytest.param(
            {"mock-text-practice"}, ["mock-text-regulatory"], id="regulatory-missing"
        ),
        pytest.param(
            set(), ["mock-text-practice", "mock-text-regulatory"], id="both-missing"
        ),
    ],
)
def test_a_deployment_missing_a_bound_corpus_refuses_to_compose(
    loaded: set[str], missing: list[str]
):
    refusing = ScriptedPolicyGate(loaded_corpora=frozenset(loaded))

    with pytest.raises(GatePluginError) as refused:
        memory_framework([PLUGIN], [ScriptedPolicyGatePlugin(refusing)])

    assert refused.value.gate_id == "policy"
    assert refused.value.reason == (
        f"the gate refuses the binding of reasoner {REASONER_ID!r}: "
        f"corpora {missing} are not loaded"
    )
    assert refusing.bindings == []
    assert refusing.closed  # the failed composition closed the gate it built


async def test_a_decided_messages_bundle_shows_the_policy_gate_ran_with_the_binding(
    client: LocalClient,
    policy_gate: ScriptedPolicyGate,
    samples: dict[str, CustomerMessage],
):
    receipt, submission = await decide_message(samples["hardship"], client=client)

    bundle = await client.get_bundle(receipt.decision_id)
    record = bundle.gate("policy")
    assert record is not None
    assert (record.status, record.mandatory) == ("ran", False)
    [request] = policy_gate.requests
    assert request.submission == submission
    assert request.gate_context == submission.gate_contexts[0]
    assert request.binding_config == POLICY_GATE_BINDING.config
    assert [corpus["corpus_id"] for corpus in request.binding_config["corpora"]] == [
        corpus_id for corpus_id, _ in CORPORA
    ]
    assert record.output is not None
    assert record.output.extra["corpora"] == [corpus_id for corpus_id, _ in CORPORA]


def test_the_make_include_loads_each_bound_corpus_from_its_own_subdirectory(
    member_dir: Path,
):
    makefile = (member_dir / "reasoner.mk").read_text(encoding="utf-8")
    loads = re.findall(
        r"policy_rag_gate\.corpus_loader --corpus-id (\S+) \\\n\s+"
        r"--reasoner-id \$\(MOCK_TEXT_REASONER_ID\) "
        r"--corpus-dir \$\(MOCK_TEXT_ASSETS\)/corpus/(\S+)$",
        makefile,
        re.MULTILINE,
    )
    variables = dict(
        re.findall(r"^(MOCK_TEXT_[A-Z_]+)\s*:=\s*(\S+)\s*$", makefile, re.MULTILINE)
    )

    assert loads == CORPORA
    [binding] = MOCK_TEXT_REGISTRATION.gates
    assert [corpus["corpus_id"] for corpus in binding.config["corpora"]] == [
        corpus_id for corpus_id, _ in CORPORA
    ]
    assert variables["MOCK_TEXT_REASONER_ID"] == REASONER_ID
    assert member_dir.parents[1] / variables["MOCK_TEXT_DIR"] == member_dir
    assert variables["MOCK_TEXT_ASSETS"] == (
        "$(MOCK_TEXT_DIR)/src/mock_text_reasoner/assets"
    )
    assert member_dir / "src" / "mock_text_reasoner" / "assets" == ASSETS_DIR
    # One loader call per corpus: the gate's one-corpus target would load the
    # whole corpus directory, both subtrees, as one corpus.
    assert not re.search(r"^POLICY_CORPUS_ID_\S*\s*[:+?]?=", makefile, re.MULTILINE)


def test_the_make_include_joins_the_mock_evals_never_the_reasoner_evals(
    member_dir: Path,
):
    makefile = (member_dir / "reasoner.mk").read_text(encoding="utf-8")
    appended = [
        (name, value)
        for name, value in re.findall(
            r"^([A-Z_]+)\s*\+=\s*(.*?)\s*$", makefile, re.MULTILINE
        )
        if name != "REASONER_HELP"
    ]

    assert appended == [
        ("REASONER_INDEX_TARGETS", "index-mock_text"),
        ("REASONER_PREPARE_TARGETS", "prepare-mock_text"),
        ("MOCK_EVAL_TARGETS", "eval-mock_text"),
    ]
    assert re.search(
        r"^eval-mock_text:\n\t.*eval\.runners\.harness "
        r"--reasoner-id \$\(MOCK_TEXT_REASONER_ID\)$",
        makefile,
        re.MULTILINE,
    )
    assert re.search(r"^prepare-mock_text: index-mock_text$", makefile, re.MULTILINE)
