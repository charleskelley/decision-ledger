"""Behavioral tests for the policy gate's plugin and its non-LLM paths.

The policy gate is a workspace member registered through the ``GatePlugin``
protocol like any gate (DR-28). These tests pin:

- the published plugin: protocol conformance, identity, its typed
  ``config_model``, and the member's own ``decisionledger.gates`` entry point
  that resolves to it;
- ``build``: everything the gate owns is constructed by the plugin, not the
  framework bootstrap — a per-reasoner profile, the prompt registry over
  every composed reasoner's ``prompts_dir``, its own autocommit Postgres
  connection on which it applies its own DDL before registering the pgvector
  adapters, the Elasticsearch client from its own settings, the models, and
  the LLM client — and a failed build releases what it opened;
- ``evaluate`` on the paths that never reach the LLM: a missing retrieval
  query fails closed before retrieval, and a template render error fails
  closed after a tenant-scoped retrieval, both with the snapshot, evidence,
  and failure stage recorded.

Per project rules the LLM path itself is not unit-tested with mocked
responses; the client here fails the test if it is ever called. The gate's
eval harness is its LLM test suite.

No Postgres, Elasticsearch, model download, or LLM API is touched.
"""

from __future__ import annotations

import importlib.metadata as metadata
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.gates import GATE_GROUP, GateKind, check_gate_plugin
from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.gate import GateOutput, GateVerdict
from core.gate_request import GateRequest
from core.submission import GateContext
from policy_rag_gate import plugin as policy_plugin
from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile
from policy_rag_gate.gate import PolicyGate, _policy_gate_output
from policy_rag_gate.models import (
    Citation,
    PolicyGateExtra,
    PolicyGateSnapshot,
    PolicyGateVerdict,
    PromptSnapshot,
)
from policy_rag_gate.models.snippet import RetrievedSnippet
from policy_rag_gate.plugin import PLUGIN, PolicyGatePlugin
from policy_rag_gate.prompt_registry import YamlPromptRegistry
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
    """Replace every infrastructure constructor the plugin uses with a fake."""
    calls: list[str] = []
    pg_conn = MagicMock(name="gate_pg_conn")
    es = MagicMock(name="es")
    embed_model = MagicMock(name="embed_model")
    cross_encoder = MagicMock(name="cross_encoder")
    llm_client = MagicMock(name="llm_client", model="fake-llm")

    def _connect(dsn: str, *, autocommit: bool = False) -> MagicMock:
        calls.append(f"connect:{dsn}:autocommit={autocommit}")
        return pg_conn

    def _apply_schema(conn: Any) -> None:
        assert conn is pg_conn
        calls.append("apply_schema")

    def _register_vector(conn: Any) -> None:
        assert conn is pg_conn
        calls.append("register_vector")

    def _elasticsearch(url: str) -> MagicMock:
        calls.append(f"elasticsearch:{url}")
        return es

    monkeypatch.setattr(policy_plugin.psycopg, "connect", _connect)
    monkeypatch.setattr(policy_plugin, "apply_schema", _apply_schema)
    monkeypatch.setattr(policy_plugin, "register_vector", _register_vector)
    monkeypatch.setattr(policy_plugin, "Elasticsearch", _elasticsearch)
    monkeypatch.setattr(policy_plugin, "SentenceTransformer", lambda n: embed_model)
    monkeypatch.setattr(policy_plugin, "CrossEncoder", lambda n: cross_encoder)

    def _openai_client(**kwargs: Any) -> MagicMock:
        calls.append(f"openai_client:{sorted(kwargs.items())}")
        return llm_client

    monkeypatch.setattr(policy_plugin, "OpenAILLMClient", _openai_client)
    return {
        "calls": calls,
        "pg_conn": pg_conn,
        "es": es,
        "embed_model": embed_model,
        "cross_encoder": cross_encoder,
        "llm_client": llm_client,
    }


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


def _plugin() -> PolicyGatePlugin:
    return PolicyGatePlugin(
        settings=PolicyRagGateSettings(
            elasticsearch_url="http://es.example:9200", openai_api_key=""
        )
    )


def test_build_opens_the_gates_own_connection_schema_index_client_and_models(
    infra, fixture_reasoner
):
    framework = _framework(fixture_reasoner)

    gate = _plugin().build(framework)

    assert isinstance(gate, PolicyGate)
    # The gate's own DDL (vector extension, policy_rag schema) runs before
    # the pgvector adapters are registered on its connection.
    assert infra["calls"] == [
        f"connect:{framework.settings.postgres_dsn}:autocommit=True",
        "apply_schema",
        "register_vector",
        "elasticsearch:http://es.example:9200",
        "openai_client:[('api_key', None)]",
    ]
    assert gate.client is infra["llm_client"]
    retriever = gate.retriever
    assert retriever._pg is infra["pg_conn"]
    assert retriever._es is infra["es"]
    assert retriever._model is infra["embed_model"]
    assert retriever._cross_encoder is infra["cross_encoder"]


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


def test_two_reasoners_shipping_one_template_id_fail_before_any_connection(
    infra, fixture_reasoner, reasoner_for
):
    other = reasoner_for("other-reasoner")  # same prompts directory

    with pytest.raises(ValueError, match="stub-v1"):
        _plugin().build(_framework(fixture_reasoner, other))

    assert infra["calls"] == []


def test_a_failed_build_closes_the_connection_it_opened(
    infra, monkeypatch, fixture_reasoner
):
    def _boom(name: str) -> None:
        raise RuntimeError("model download failed")

    monkeypatch.setattr(policy_plugin, "SentenceTransformer", _boom)

    with pytest.raises(RuntimeError, match="model download failed"):
        _plugin().build(_framework(fixture_reasoner))

    infra["pg_conn"].close.assert_called_once()


def test_a_failed_llm_client_closes_the_index_client_and_the_connection(
    infra, monkeypatch, fixture_reasoner
):
    def _no_key(**_: Any) -> None:
        raise RuntimeError("api_key must be set")

    monkeypatch.setattr(policy_plugin, "OpenAILLMClient", _no_key)

    with pytest.raises(RuntimeError, match="api_key"):
        _plugin().build(_framework(fixture_reasoner))

    infra["es"].close.assert_called_once()
    infra["pg_conn"].close.assert_called_once()


def test_close_releases_the_index_client_and_the_connection(infra, fixture_reasoner):
    gate = _plugin().build(_framework(fixture_reasoner))

    gate.close()

    infra["es"].close.assert_called_once()
    infra["pg_conn"].close.assert_called_once()


# ---------------------------------------------------------------------------
# evaluate — paths that fail closed before the LLM
# ---------------------------------------------------------------------------


class _ForbiddenClient:
    """LLM client that fails the test if the gate ever calls it."""

    model = "never-called"

    async def complete_structured(self, **_: Any) -> Any:
        raise AssertionError("the LLM must not be called on this path")


class _RecordingRetriever:
    """Retriever fake returning two snippets from two conflict groups."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> tuple[list[RetrievedSnippet], str]:
        self.calls.append(kwargs)
        return [
            _snippet("STUB-POLICY-A", "REGION_A"),
            _snippet("STUB-POLICY-B", "REGION_B"),
        ], "rrf_only"


def _snippet(document_id: str, jurisdiction: str) -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id=document_id,
        title=f"Policy {document_id}",
        version="1.0",
        jurisdiction=jurisdiction,
        section_path="2. Calibration Review",
        text="Widgets with a thin history SHALL be held.",
        relevance_score=0.5,
        retrieval_path="rrf_only",
    )


def _gate(retriever: _RecordingRetriever, prompts_dir: Any) -> PolicyGate:
    return PolicyGate(
        client=_ForbiddenClient(),  # type: ignore[arg-type]
        prompt_registry=YamlPromptRegistry([prompts_dir]),
        retriever=retriever,  # type: ignore[arg-type]
    )


def _request(submission: Any, **config: Any) -> GateRequest:
    context = GateContext(gate_id="policy", gate_config=config)
    return GateRequest(
        decision_id="dec-policy-001",
        submission=submission.model_copy(update={"gate_contexts": (context,)}),
        gate_context=context,
        budget_ms=1_000,
    )


async def test_a_missing_retrieval_query_fails_closed_before_retrieving(
    submission, fixture_reasoner
):
    retriever = _RecordingRetriever()

    output = await _gate(retriever, fixture_reasoner.prompts_dir).evaluate(
        _request(submission, template_id="stub-v1", template_vars={})
    )

    assert type(output) is GateOutput
    assert output.gate_id == "policy"
    assert output.deterministic is False
    assert output.verdict is None
    assert output.evidence == ()
    assert retriever.calls == []
    extra = PolicyGateExtra.model_validate(output.extra, strict=False)
    assert extra.failure == "missing_retrieval_query"
    snapshot = PolicyGateSnapshot.model_validate(output.evidence_snapshot, strict=False)
    assert snapshot.retrieval_path == "skipped"
    assert snapshot.prompt_template_id == "stub-v1"


async def test_a_blank_retrieval_query_fails_closed_before_retrieving(
    submission, fixture_reasoner
):
    retriever = _RecordingRetriever()

    output = await _gate(retriever, fixture_reasoner.prompts_dir).evaluate(
        _request(submission, template_id="stub-v1", retrieval_query="   ")
    )

    assert output.verdict is None
    assert retriever.calls == []
    extra = PolicyGateExtra.model_validate(output.extra, strict=False)
    assert extra.failure == "missing_retrieval_query"


async def test_a_render_error_fails_closed_after_a_tenant_scoped_retrieval(
    submission, fixture_reasoner
):
    retriever = _RecordingRetriever()

    output = await _gate(retriever, fixture_reasoner.prompts_dir).evaluate(
        _request(
            submission,
            template_id="stub-v1",
            # The fixture template needs widget_score, thin_history, and region.
            template_vars={"widget_score": "0.5"},
            retrieval_query="calibration review",
            jurisdictions=["REGION_A", "REGION_B"],
            risk_tier="STANDARD",
            corpus_version="stub-corpus-v1",
        )
    )

    assert output.verdict is None
    (call,) = retriever.calls
    assert call["query"] == "calibration review"
    assert call["reasoner_id"] == "stub-reasoner"
    assert call["jurisdictions"] == ["REGION_A", "REGION_B"]
    assert call["risk_tier"] == "STANDARD"
    extra = PolicyGateExtra.model_validate(output.extra, strict=False)
    assert extra.failure == "render_error"
    assert extra.token_cost is None
    snapshot = PolicyGateSnapshot.model_validate(output.evidence_snapshot, strict=False)
    assert snapshot.retrieval_query == "calibration review"
    assert [s.document_id for s in snapshot.retrieval_results] == [
        "STUB-POLICY-A",
        "STUB-POLICY-B",
    ]
    assert snapshot.retrieval_path == "rrf_only"
    assert snapshot.corpus_version == "stub-corpus-v1"
    assert snapshot.rendered_prompt == ""
    # Evidence carries each retrieved snippet's jurisdiction, so the
    # jurisdiction tier sees exactly the retrieved set.
    assert output.evidence_jurisdictions() == frozenset({"REGION_A", "REGION_B"})
    assert [e.source_id for e in output.evidence] == ["STUB-POLICY-A", "STUB-POLICY-B"]


async def test_absent_filters_reach_the_retriever_as_no_filter(
    submission, fixture_reasoner
):
    retriever = _RecordingRetriever()

    await _gate(retriever, fixture_reasoner.prompts_dir).evaluate(
        _request(submission, template_id="stub-v1", retrieval_query="q")
    )

    (call,) = retriever.calls
    assert call["jurisdictions"] is None
    assert call["risk_tier"] is None


async def test_an_unknown_template_propagates(submission, fixture_reasoner):
    with pytest.raises(KeyError):
        await _gate(_RecordingRetriever(), fixture_reasoner.prompts_dir).evaluate(
            _request(submission, template_id="no-such-template", retrieval_query="q")
        )


# ---------------------------------------------------------------------------
# The success-path output builder (pure; the LLM call itself is eval-tested)
# ---------------------------------------------------------------------------


def test_a_verdict_becomes_the_universal_output_with_the_explanation_in_extra():
    snippets = [
        _snippet("STUB-POLICY-A", "REGION_A"),
        _snippet("STUB-POLICY-B", "REGION_B").model_copy(
            update={"relevance_score": -4.32, "retrieval_path": "reranked"}
        ),
    ]
    citation = Citation(policy_id="STUB-POLICY-A", snippet="text", relevance="why")
    verdict = PolicyGateVerdict(
        permitted_actions=[DecisionAction.CHALLENGE],
        required_controls=["step_up"],
        rationale="Thin history; step up.",
        citations=[citation],
        confidence=0.8,
        escalate_to_human=False,
        escalation_reason=None,
    )
    snapshot = PolicyGateSnapshot(
        retrieval_query="calibration review",
        retrieval_results=snippets,
        retrieval_path="reranked",
        index_version="unknown",
        model_version="m",
        prompt_template_id="stub-v1",
        prompt_template_version="1.0.0",
        corpus_version="c",
        rendered_prompt="rendered",
        prompt_snapshot=PromptSnapshot(
            template_id="stub-v1", version="1.0.0", template_text="t"
        ),
        template_vars={},
    )
    extra = PolicyGateExtra(
        rationale=verdict.rationale, citations=[citation], retrieval_ms=2.0
    )

    output = _policy_gate_output(
        verdict=verdict, snippets=snippets, snapshot=snapshot, extra=extra
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
    assert output.evidence_jurisdictions() == frozenset({"REGION_A", "REGION_B"})
