"""Behavioral tests for the policy gate's plugin and its non-LLM paths.

The policy gate is a workspace member registered through the ``GatePlugin``
protocol like any gate (DR-28). These tests pin:

- the published plugin: protocol conformance, identity, its typed
  ``config_model`` and ``binding_config_model`` (``PolicyBindingConfig``,
  DR-34), and the member's own ``decisionledger.gates`` entry point that
  resolves to it;
- ``build``: everything the gate owns is constructed by the plugin, not the
  framework bootstrap — a per-reasoner profile, the prompt registry over
  every composed reasoner's ``prompts_dir``, the LLM client, and the
  retrieval providers of the gate's own entry-point group, none of which is
  opened until a binding names it;
- ``evaluate`` on the paths that never reach the LLM: a missing retrieval
  query fails closed before retrieval, and a template render error fails
  closed after retrieving from every corpus the request's binding names,
  with the per-corpus queries, the passages under the evidence contract,
  the evidence references, and the failure stage recorded — two corpora at
  two tiers included.

Per project rules the LLM path itself is not unit-tested with mocked
responses; the client here fails the test if it is ever called. The gate's
eval harness is its LLM test suite.

No Postgres, Elasticsearch, model download, or LLM API is touched.
"""

from __future__ import annotations

import importlib.metadata as metadata
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.gates import GATE_GROUP, GateKind, check_gate_plugin
from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.exceptions import GateConfigError
from core.gate import GateOutput, GateVerdict
from core.gate_request import GateRequest
from core.submission import GateContext
from policy_rag_gate import plugin as policy_plugin
from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile
from policy_rag_gate.corpus import PolicyBindingConfig
from policy_rag_gate.gate import PolicyGate, _held, _policy_gate_output
from policy_rag_gate.models import (
    Citation,
    PolicyGateExtra,
    PolicyGateSnapshot,
    PromptSnapshot,
    TieredPolicyGateVerdict,
    content_hash,
)
from policy_rag_gate.plugin import PLUGIN, PolicyGatePlugin
from policy_rag_gate.prompt_registry import YamlPromptRegistry
from policy_rag_gate.providers import (
    ProviderContractError,
    ProviderRegistry,
)
from policy_rag_gate.providers.reference import PLUGIN as REFERENCE_PLUGIN
from policy_rag_gate.settings import PolicyRagGateSettings

# ---------------------------------------------------------------------------
# The published plugin
# ---------------------------------------------------------------------------


def test_the_policy_gate_plugin_satisfies_the_gate_plugin_protocol():
    assert check_gate_plugin(PLUGIN, name="policy") is PLUGIN
    assert PLUGIN.gate_id == "policy"
    assert PLUGIN.kind == GateKind.LLM
    assert PLUGIN.deterministic is False
    assert PLUGIN.config_model is PolicyGateConfig
    assert PLUGIN.binding_config_model is PolicyBindingConfig


def test_the_members_entry_point_resolves_to_the_policy_gate_plugin():
    # The gate member publishes itself; the framework package publishes no
    # plugin (the Phase 1a interim entry point left with the gate).
    (entry,) = [
        ep for ep in metadata.entry_points(group=GATE_GROUP) if ep.name == "policy"
    ]
    assert entry.value == "policy_rag_gate.plugin:PLUGIN"
    assert entry.dist is not None
    assert entry.dist.name == "decisionledger-gate-policy-rag"
    assert entry.load() is PLUGIN


# ---------------------------------------------------------------------------
# build — the gate constructs what it owns
# ---------------------------------------------------------------------------


@pytest.fixture
def infra(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the LLM client the plugin builds; the providers stay unbuilt."""
    calls: list[str] = []
    llm_client = MagicMock(name="llm_client", model="fake-llm")

    def _openai_client(**kwargs: Any) -> MagicMock:
        calls.append(f"openai_client:{sorted(kwargs.items())}")
        return llm_client

    monkeypatch.setattr(policy_plugin, "OpenAILLMClient", _openai_client)
    return {"calls": calls, "llm_client": llm_client}


def _framework(*reasoners: Any) -> Any:
    framework = MagicMock(name="framework")
    framework.settings = FrameworkSettings(
        postgres_host="db.example",
        postgres_port=5433,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
    )
    framework.reasoners = reasoners
    return framework


def _plugin(assets_root: str = "") -> PolicyGatePlugin:
    # Every setting is pinned, so a developer's .env cannot leak into a build.
    return PolicyGatePlugin(
        settings=PolicyRagGateSettings(
            elasticsearch_url="http://es.example:9200",
            openai_api_key="",
            assets_root=assets_root,
        )
    )


def test_build_opens_no_provider_until_a_binding_names_one(infra, fixture_reasoner):
    gate = _plugin().build(_framework(fixture_reasoner))

    assert isinstance(gate, PolicyGate)
    assert infra["calls"] == ["openai_client:[('api_key', None)]"]
    assert gate.client is infra["llm_client"]
    assert isinstance(gate.providers, ProviderRegistry)
    # The member's own provider group is discovered, nothing is built yet.
    assert gate.providers.provider_ids() == ["reference"]
    assert gate.providers._built == {}


def test_the_gate_escrows_through_the_frameworks_store(infra, fixture_reasoner):
    # DR-34 part 2: the plugin hands the gate the store's evidence escrow;
    # the gate never reads the escrow setting itself.
    framework = _framework(fixture_reasoner)

    gate = _plugin().build(framework)

    assert gate.escrow is framework.store.artifacts


def test_providers_are_built_with_the_frameworks_dsn_and_the_gates_settings(
    infra, monkeypatch, fixture_reasoner, stub_provider
):
    seen: list[Any] = []

    def _build(services: Any) -> Any:
        seen.append(services)
        return stub_provider

    monkeypatch.setattr(REFERENCE_PLUGIN, "build", _build)
    framework = _framework(fixture_reasoner)
    gate = _plugin().build(framework)

    assert gate.providers.get("reference") is stub_provider
    (services,) = seen
    assert services.postgres_dsn == framework.settings.postgres_dsn
    assert services.settings.elasticsearch_url == "http://es.example:9200"


def test_build_hands_the_settings_key_to_the_llm_client(infra, fixture_reasoner):
    plugin = PolicyGatePlugin(
        settings=PolicyRagGateSettings(
            elasticsearch_url="http://es.example:9200", openai_api_key="sk-gate"
        )
    )

    plugin.build(_framework(fixture_reasoner))

    assert infra["calls"][-1] == "openai_client:[('api_key', 'sk-gate')]"


def test_settings_are_read_lazily_from_the_environment(monkeypatch):
    # Built before the variable exists, as the entry point's PLUGIN is at
    # import time: the settings are read on first use, not at construction.
    monkeypatch.delenv("POLICY_RAG_GATE_ELASTICSEARCH_URL", raising=False)
    plugin = PolicyGatePlugin()
    monkeypatch.setenv("POLICY_RAG_GATE_ELASTICSEARCH_URL", "http://env.example:9200")

    assert plugin.settings.elasticsearch_url == "http://env.example:9200"


def test_build_loads_the_prompt_registry_from_every_composed_reasoner(
    infra, fixture_reasoner
):
    gate = _plugin().build(_framework(fixture_reasoner))

    assert isinstance(gate.prompt_registry, YamlPromptRegistry)
    assert gate.prompt_registry.registered_ids() == ["stub-v1"]


def test_build_derives_what_each_composed_reasoner_may_submit(
    infra, fixture_reasoner, reasoner_for, tmp_path
):
    # A second reasoner with no prompts and no corpus may submit nothing.
    empty = reasoner_for("empty-reasoner", prompts_dir=tmp_path, corpus_dir=tmp_path)

    gate = _plugin().build(_framework(fixture_reasoner, empty))

    assert gate.profiles == {
        "stub-reasoner": ReasonerProfile(
            template_ids=frozenset({"stub-v1"}),
            jurisdictions=frozenset({"REGION_A", "REGION_B"}),
        ),
        "empty-reasoner": ReasonerProfile(
            template_ids=frozenset(), jurisdictions=frozenset()
        ),
    }


def test_build_hands_the_asset_root_to_the_gate(infra, fixture_reasoner, tmp_path):
    gate = _plugin(assets_root=str(tmp_path)).build(_framework(fixture_reasoner))

    assert gate.assets_root == tmp_path
    assert _plugin().build(_framework(fixture_reasoner)).assets_root is None


def test_a_blank_asset_root_is_no_asset_root():
    """A blank variable is unset, never the current directory."""
    assert PolicyRagGateSettings(assets_root="").assets_root_path is None
    assert PolicyRagGateSettings(assets_root="  ").assets_root_path is None
    assert PolicyRagGateSettings(assets_root="/srv/assets").assets_root_path == (
        Path("/srv/assets")
    )


def test_two_reasoners_shipping_one_template_id_fail_the_build(
    infra, fixture_reasoner, reasoner_for
):
    other = reasoner_for("other-reasoner")  # same prompts directory

    with pytest.raises(ValueError, match="stub-v1"):
        _plugin().build(_framework(fixture_reasoner, other))

    assert infra["calls"] == []


def test_close_releases_the_providers_the_gate_built(
    infra, monkeypatch, fixture_reasoner, stub_provider
):
    monkeypatch.setattr(REFERENCE_PLUGIN, "build", lambda _services: stub_provider)
    gate = _plugin().build(_framework(fixture_reasoner))
    gate.providers.get("reference")

    gate.close()

    assert stub_provider.closed is True


# ---------------------------------------------------------------------------
# evaluate — paths that fail closed before the LLM
# ---------------------------------------------------------------------------


class _ForbiddenClient:
    """LLM client that fails the test if the gate ever calls it."""

    model = "never-called"

    async def complete_structured(self, **_: Any) -> Any:
        raise AssertionError("the LLM must not be called on this path")


def _gate(provider: Any, prompts_dir: Any) -> PolicyGate:
    return PolicyGate(
        client=_ForbiddenClient(),  # type: ignore[arg-type]
        prompt_registry=YamlPromptRegistry([prompts_dir]),
        providers=ProviderRegistry.of(provider),
    )


def _request(
    submission: Any, *, binding: dict[str, Any] | None = None, **config: Any
) -> GateRequest:
    context = GateContext(gate_id="policy", gate_config=config)
    return GateRequest(
        decision_id="dec-policy-001",
        submission=submission.model_copy(update={"gate_contexts": (context,)}),
        gate_context=context,
        budget_ms=1_000,
        binding_config=binding
        if binding is not None
        else {"corpora": [{"corpus_id": "stub-policy", "tier": 1}]},
    )


async def test_a_missing_retrieval_query_fails_closed_before_retrieving(
    submission, fixture_reasoner, stub_provider
):
    output = await _gate(stub_provider, fixture_reasoner.prompts_dir).evaluate(
        _request(submission, template_id="stub-v1", template_vars={})
    )

    assert type(output) is GateOutput
    assert output.gate_id == "policy"
    assert output.deterministic is False
    assert output.verdict is None
    assert output.evidence == ()
    assert stub_provider.calls == []
    extra = PolicyGateExtra.model_validate(output.extra, strict=False)
    assert extra.failure == "missing_retrieval_query"
    snapshot = PolicyGateSnapshot.model_validate(output.evidence_snapshot, strict=False)
    assert snapshot.snapshot_version == 2
    assert snapshot.corpus_queries == []
    assert snapshot.contexts == []
    assert snapshot.prompt_template_id == "stub-v1"


async def test_a_blank_retrieval_query_fails_closed_before_retrieving(
    submission, fixture_reasoner, stub_provider
):
    output = await _gate(stub_provider, fixture_reasoner.prompts_dir).evaluate(
        _request(submission, template_id="stub-v1", retrieval_query="   ")
    )

    assert output.verdict is None
    assert stub_provider.calls == []
    extra = PolicyGateExtra.model_validate(output.extra, strict=False)
    assert extra.failure == "missing_retrieval_query"


async def test_a_render_error_fails_closed_after_retrieving_from_the_bound_corpus(
    submission, fixture_reasoner, stub_provider
):
    output = await _gate(stub_provider, fixture_reasoner.prompts_dir).evaluate(
        _request(
            submission,
            template_id="stub-v1",
            # The fixture template needs widget_score, thin_history, and region.
            template_vars={"widget_score": "0.5"},
            retrieval_query="calibration review",
            jurisdictions=["REGION_A", "REGION_B"],
            risk_tier="STANDARD",
        )
    )

    assert output.verdict is None
    (call,) = stub_provider.calls
    assert call == {
        "query": "calibration review",
        "corpus_id": "stub-policy",
        "tier": 1,
        "k": 5,
        "jurisdictions": ["REGION_A", "REGION_B"],
        "risk_tier": "STANDARD",
    }
    extra = PolicyGateExtra.model_validate(output.extra, strict=False)
    assert extra.failure == "render_error"
    assert extra.token_cost is None
    snapshot = PolicyGateSnapshot.model_validate(output.evidence_snapshot, strict=False)
    assert snapshot.retrieval_query == "calibration review"
    (query,) = snapshot.corpus_queries
    assert query.corpus_ref.corpus_id == "stub-policy"
    assert query.state is not None
    assert query.state.provenance_level == "pinned_version"
    assert query.ranking_applied == "reranked"
    assert query.returned == 2
    assert [c.document_id for c in snapshot.contexts] == [
        "STUB-POLICY-A",
        "STUB-POLICY-B",
    ]
    assert snapshot.evidence_layout == "numbered_list"
    assert snapshot.dedup_merged == 0
    assert snapshot.rendered_prompt == ""
    # Evidence carries each rendered passage's jurisdiction, so the
    # jurisdiction tier sees exactly the rendered set, and its digest.
    assert output.evidence_jurisdictions() == frozenset({"REGION_A", "REGION_B"})
    assert [e.source_id for e in output.evidence] == ["STUB-POLICY-A", "STUB-POLICY-B"]
    assert [e.content_hash for e in output.evidence] == [
        c.content_hash for c in snapshot.contexts
    ]


async def test_two_corpora_bound_at_two_tiers_are_rendered_by_tier(
    submission, fixture_reasoner, fake_provider, fake_passage, tmp_path
):
    # A template with the conflict/gap protocol renders evidence in tiers.
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "tiered-v1.yaml").write_text(
        "template_id: tiered-v1\nversion: 1.0.0\ndescription: d\n"
        "evidence_layout: tiered_blocks\n"
        "template: |\n  Case {widget_score}\n  {policy_snippets}\n",
        encoding="utf-8",
    )
    provider = fake_provider(
        {
            "practice": [
                fake_passage("PN-1", "Challenge thin histories."),
                fake_passage("PN-2", "Shared passage."),
            ],
            "regulatory": [
                fake_passage("REG-1", "Hold thin histories."),
                fake_passage("REG-2", "Shared passage."),
            ],
        }
    )
    binding = {
        "corpora": [
            {"corpus_id": "practice", "tier": 2},
            {"corpus_id": "regulatory", "tier": 1},
        ],
        "k_per_corpus": 2,
    }

    output = await _gate(provider, prompts).evaluate(
        _request(
            submission,
            binding=binding,
            template_id="tiered-v1",
            template_vars={},  # missing widget_score: fails closed at render
            retrieval_query="thin history",
        )
    )

    snapshot = PolicyGateSnapshot.model_validate(output.evidence_snapshot, strict=False)
    assert [q.corpus_ref.corpus_id for q in snapshot.corpus_queries] == [
        "practice",
        "regulatory",
    ]
    assert [(c.tier, c.document_id) for c in snapshot.contexts] == [
        (1, "REG-1"),
        (1, "REG-2"),
        (2, "PN-1"),
    ]
    assert snapshot.evidence_layout == "tiered_blocks"
    assert snapshot.dedup_merged == 1
    assert [q.merged for q in snapshot.corpus_queries] == [1, 0]


async def test_absent_filters_reach_the_provider_as_no_filter(
    submission, fixture_reasoner, stub_provider
):
    await _gate(stub_provider, fixture_reasoner.prompts_dir).evaluate(
        _request(submission, template_id="stub-v1", retrieval_query="q")
    )

    (call,) = stub_provider.calls
    assert call["jurisdictions"] is None
    assert call["risk_tier"] is None


async def test_an_unknown_template_propagates(
    submission, fixture_reasoner, stub_provider
):
    with pytest.raises(KeyError):
        await _gate(stub_provider, fixture_reasoner.prompts_dir).evaluate(
            _request(submission, template_id="no-such-template", retrieval_query="q")
        )


async def test_a_request_without_a_binding_configuration_is_refused(
    submission, fixture_reasoner, stub_provider
):
    with pytest.raises(GateConfigError, match="corpora"):
        await _gate(stub_provider, fixture_reasoner.prompts_dir).evaluate(
            _request(submission, binding={}, template_id="stub-v1", retrieval_query="q")
        )
    assert stub_provider.calls == []


async def test_a_provider_error_propagates(submission, fixture_reasoner, fake_provider):
    provider = fake_provider(
        {"stub-policy": []}, retrieve_error=ConnectionError("index down")
    )

    with pytest.raises(ConnectionError, match="index down"):
        await _gate(provider, fixture_reasoner.prompts_dir).evaluate(
            _request(submission, template_id="stub-v1", retrieval_query="q")
        )


async def test_a_provider_breaking_the_evidence_contract_propagates(
    submission, fixture_reasoner, fake_provider, fake_passage
):
    provider = fake_provider(
        {"stub-policy": [fake_passage("DOC-A", "a")]},
        tamper=lambda result: result.model_copy(
            update={
                "contexts": tuple(
                    c.model_copy(update={"tier": 3}) for c in result.contexts
                )
            }
        ),
    )

    with pytest.raises(ProviderContractError, match="tier 3"):
        await _gate(provider, fixture_reasoner.prompts_dir).evaluate(
            _request(submission, template_id="stub-v1", retrieval_query="q")
        )


def test_a_submission_can_no_longer_label_a_corpus_version():
    # The gate records the state of every corpus it queries itself (DR-34).
    with pytest.raises(ValidationError, match="corpus_version"):
        PolicyGateConfig.model_validate(
            {"template_id": "stub-v1", "corpus_version": "stub-corpus-v1"}
        )


# ---------------------------------------------------------------------------
# The success-path output builder (pure; the LLM call itself is eval-tested)
# ---------------------------------------------------------------------------


def _snapshot(contexts: list[Any]) -> PolicyGateSnapshot:
    return PolicyGateSnapshot(
        retrieval_query="calibration review",
        corpus_queries=[],
        contexts=contexts,
        evidence_layout="tiered_blocks",
        dedup_merged=0,
        index_version="unknown",
        model_version="m",
        prompt_template_id="stub-v1",
        prompt_template_version="1.0.0",
        rendered_prompt="rendered",
        prompt_snapshot=PromptSnapshot(
            template_id="stub-v1", version="1.0.0", template_text="t"
        ),
        template_vars={},
    )


def test_a_verdict_becomes_the_universal_output_with_the_explanation_in_extra(
    retrieved_context,
):
    negative = retrieved_context.model_copy(
        update={"scores": {"rerank": -4.32}, "document_id": "STUB-POLICY-B"}
    )
    contexts = [retrieved_context, negative]
    citation = Citation(policy_id="STUB-POLICY-A", snippet="text", relevance="why")
    verdict = TieredPolicyGateVerdict(
        permitted_actions=[DecisionAction.CHALLENGE],
        required_controls=["step_up"],
        rationale="Thin history; step up.",
        citations=[citation],
        confidence=0.8,
        escalate_to_human=False,
        escalation_reason=None,
        governing_corpus_id="stub-policy",
        conflict_detected=False,
        evidence_sufficient=True,
    )
    snapshot = _snapshot(contexts)
    extra = PolicyGateExtra(
        rationale=verdict.rationale,
        citations=[citation],
        retrieval_ms=2.0,
        governing_corpus_id="stub-policy",
        conflict_detected=False,
        evidence_sufficient=True,
    )

    output = _policy_gate_output(
        verdict=verdict.universal(), contexts=contexts, snapshot=snapshot, extra=extra
    )

    # Only the universal verdict is stored; the explanation rides in extra.
    assert type(output.verdict) is GateVerdict
    assert output.verdict == verdict.universal()
    assert PolicyGateExtra.model_validate(output.extra, strict=False) == extra
    # The snapshot re-validates, negative reranker logits included.
    assert (
        PolicyGateSnapshot.model_validate(output.evidence_snapshot, strict=False)
        == snapshot
    )
    assert [e.content_hash for e in output.evidence] == [
        content_hash(retrieved_context.text)
    ] * 2


def test_a_held_verdict_keeps_what_the_model_returned_in_extra(retrieved_context):
    verdict = TieredPolicyGateVerdict(
        permitted_actions=[DecisionAction.ALLOW],
        required_controls=[],
        rationale="Passages disagree.",
        citations=[],
        confidence=0.7,
        escalate_to_human=False,
        escalation_reason=None,
        governing_corpus_id="stub-policy",
        conflict_detected=True,
        evidence_sufficient=True,
    )
    held, hold = _held(verdict)
    extra = PolicyGateExtra(
        rationale=verdict.rationale,
        governing_corpus_id="stub-policy",
        conflict_detected=True,
        evidence_sufficient=True,
        protocol_hold=hold,
        verdict_as_returned=verdict.universal(),
    )

    output = _policy_gate_output(
        verdict=held,
        contexts=[retrieved_context],
        snapshot=_snapshot([retrieved_context]),
        extra=extra,
    )

    assert output.verdict is not None
    assert output.verdict.permitted_actions == [DecisionAction.HOLD]
    stored = PolicyGateExtra.model_validate(output.extra, strict=False)
    assert stored.protocol_hold == "conflict_detected"
    assert stored.verdict_as_returned == verdict.universal()
