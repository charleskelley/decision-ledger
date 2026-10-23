"""The asset-root layout: a copy of ``assets/`` is the mock's directory under a root.

A ledger that registers the mock by file or over the API, its plugin
excluded, serves the mock's prompts and corpus from the RAG policy gate's
asset root (``<root>/mock-stream-reasoner/``). These tests run the gate's own
loader over a copy of ``assets/`` and its own models over what the mock
sends: the profile matches the plugin's, every gate context the mock builds
validates and is authorized, the registration's binding is one the gate's
binding model takes, and the member's scripted double takes and refuses the
same configurations as the gate it stands in for.
"""

from __future__ import annotations

import shutil

import pytest
from pydantic import ValidationError

from mock_stream_reasoner.assembler import build_submission
from mock_stream_reasoner.plugin import ASSETS_DIR, PLUGIN
from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION
from mock_stream_reasoner.scorer import score_transfer
from mock_stream_reasoner.state import AccountWindows
from mock_stream_reasoner.testing import ScriptedBindingConfig, ScriptedGateConfig

policy_rag_gate = pytest.importorskip("policy_rag_gate")

REASONER_ID = MOCK_STREAM_REGISTRATION.reasoner_id


@pytest.fixture
def asset_root(tmp_path):
    """An asset root holding a copy of ``assets/`` as the mock's directory."""
    shutil.copytree(ASSETS_DIR, tmp_path / REASONER_ID)
    return tmp_path


def _submissions(transfer):
    """A fresh account's large first transfer (gated) and a steady one (recorded)."""
    windows = AccountWindows()
    fresh = transfer(1, "acct-fresh", 500.0)
    gated = build_submission(fresh, score_transfer(windows.observe(fresh)))
    *history, steady = [
        transfer(n, "acct-steady", 100.0, minutes=20.0 * n) for n in range(2, 6)
    ]
    for earlier in history:
        windows.observe(earlier)
    recorded = build_submission(steady, score_transfer(windows.observe(steady)))
    assert (gated.mode, recorded.mode) == ("gate", "record")
    return [recorded, gated]


def test_the_assets_directory_holds_only_the_asset_root_layout():
    assert sorted(p.name for p in ASSETS_DIR.iterdir()) == ["corpus", "prompts"]
    assert all((ASSETS_DIR / name).is_dir() for name in ("corpus", "prompts"))


def test_the_gate_loads_a_copy_of_the_assets_from_an_asset_root(asset_root):
    from policy_rag_gate.assets import load_root_assets
    from policy_rag_gate.config import ReasonerProfile
    from policy_rag_gate.prompt_registry import DEFAULT_SYSTEM_TEXT

    profile, templates = load_root_assets(asset_root, REASONER_ID)

    assert profile.template_ids == frozenset({"mock-stream-v1"})
    assert profile.jurisdictions == frozenset({"GLOBAL"})
    assert profile == ReasonerProfile.from_assets(
        prompts_dir=PLUGIN.prompts_dir, corpus_dir=PLUGIN.corpus_dir
    )
    template, path = templates["mock-stream-v1"]
    assert path == asset_root / REASONER_ID / "prompts" / "mock-stream-v1.yaml"
    assert template.evidence_layout == "tiered_blocks"
    assert "{policy_snippets}" in template.template_text
    assert template.system_text != DEFAULT_SYSTEM_TEXT


def test_every_corpus_document_has_complete_policy_document_front_matter():
    from policy_rag_gate.corpus_loader import parse_document

    documents = [
        parse_document(path)[0] for path in sorted(PLUGIN.corpus_dir.rglob("*.md"))
    ]

    assert sorted(document.policy_id for document in documents) == [
        "MS-PRAC-NEW-ACCOUNT",
        "MS-PRAC-VELOCITY",
    ]
    assert {document.jurisdiction for document in documents} == {"GLOBAL"}


def test_every_policy_context_the_mock_sends_is_one_the_gate_validates_and_authorizes(
    asset_root, transfer
):
    from policy_rag_gate.assets import load_root_assets
    from policy_rag_gate.config import PolicyGateConfig

    profile, templates = load_root_assets(asset_root, REASONER_ID)
    [(template, _)] = templates.values()

    for submission in _submissions(transfer):
        [context] = submission.gate_contexts
        config = PolicyGateConfig.model_validate(context.gate_config)
        profile.authorize(config)
        assert config.template_id == template.template_id
        assert template.required_vars <= set(config.template_vars)


def test_the_registrations_binding_is_one_the_policy_gate_takes():
    from policy_rag_gate.corpus import PolicyBindingConfig

    binding = MOCK_STREAM_REGISTRATION.binding("policy")
    assert binding is not None

    config = PolicyBindingConfig.model_validate(binding.config)

    assert [
        (corpus.corpus_id, corpus.provider, corpus.tier, corpus.min_provenance)
        for corpus in config.corpora
    ] == [("mock-stream-practices", "reference", 2, "pinned_version")]
    assert config.k_per_corpus == 5


def test_the_double_takes_the_same_configurations_as_the_policy_gate(transfer):
    from policy_rag_gate.config import PolicyGateConfig
    from policy_rag_gate.corpus import PolicyBindingConfig

    binding = MOCK_STREAM_REGISTRATION.binding("policy")
    assert binding is not None
    assert ScriptedBindingConfig.model_validate(binding.config).model_dump(
        mode="json"
    ) == PolicyBindingConfig.model_validate(binding.config).model_dump(mode="json")
    for submission in _submissions(transfer):
        gate_config = submission.gate_contexts[0].gate_config
        assert ScriptedGateConfig.model_validate(gate_config).model_dump(
            mode="json"
        ) == PolicyGateConfig.model_validate(gate_config).model_dump(mode="json")


_CORPUS = {"corpus_id": "mock-stream-practices", "tier": 2}
_CONTEXT = {
    "template_id": "mock-stream-v1",
    "template_vars": {"amount": "500.00"},
    "retrieval_query": "transfer review",
    "jurisdictions": ["GLOBAL"],
}


@pytest.mark.parametrize(
    ("model", "config"),
    [
        pytest.param("binding", {"corpora": []}, id="binding-without-a-corpus"),
        pytest.param(
            "binding", {"corpora": [{**_CORPUS, "tier": "2"}]}, id="tier-as-text"
        ),
        pytest.param(
            "binding", {"corpora": [{**_CORPUS, "tier": 0}]}, id="tier-below-one"
        ),
        pytest.param(
            "binding",
            {"corpora": [_CORPUS, {**_CORPUS, "tier": 1}]},
            id="a-corpus-bound-twice",
        ),
        pytest.param(
            "binding",
            {"corpora": [{**_CORPUS, "min_provenance": "hearsay"}]},
            id="unknown-provenance",
        ),
        pytest.param(
            "binding", {"corpora": [_CORPUS], "k_per_corpus": 0}, id="k-below-one"
        ),
        pytest.param(
            "binding",
            {"corpora": [{**_CORPUS, "jurisdictions": [""]}]},
            id="a-blank-jurisdiction",
        ),
        pytest.param(
            "binding",
            {"corpora": [_CORPUS], "corpus_version": "v1"},
            id="binding-unknown-key",
        ),
        pytest.param(
            "context",
            {k: v for k, v in _CONTEXT.items() if k != "template_id"},
            id="context-without-a-template",
        ),
        pytest.param(
            "context",
            {**_CONTEXT, "template_vars": {"amount": 500}},
            id="a-variable-that-is-not-text",
        ),
        pytest.param(
            "context", {**_CONTEXT, "corpus_version": "v1"}, id="context-unknown-key"
        ),
    ],
)
def test_the_double_refuses_the_configurations_the_policy_gate_refuses(model, config):
    from policy_rag_gate.config import PolicyGateConfig
    from policy_rag_gate.corpus import PolicyBindingConfig

    real, double = {
        "binding": (PolicyBindingConfig, ScriptedBindingConfig),
        "context": (PolicyGateConfig, ScriptedGateConfig),
    }[model]

    with pytest.raises(ValidationError):
        real.model_validate(config)
    with pytest.raises(ValidationError):
        double.model_validate(config)
