"""Behavioral tests for the policy gate's plugin and its non-LLM paths.

The policy gate is registered through the same ``GatePlugin`` protocol as
any gate (DR-28), via an interim entry point on the framework package. These
tests pin:

- the published plugin: protocol conformance, identity, and the installed
  ``decisionledger.gates`` entry point that resolves to it;
- ``build``: everything the gate owns is constructed by the plugin, not the
  framework bootstrap — its own autocommit Postgres connection with pgvector
  adapters, the Elasticsearch client, the models, the prompt registry over
  every composed reasoner's ``prompts_dir``, and the LLM client — and a
  failed build releases what it opened;
- ``evaluate`` on the paths that never reach the LLM: a missing retrieval
  query fails closed before retrieval, and a template render error fails
  closed after a tenant-scoped retrieval, both with the snapshot, evidence,
  and failure stage recorded.

Per project rules the LLM path itself is not unit-tested with mocked
responses; the client here fails the test if it is ever called. The eval
harness is the policy gate's LLM test suite.

No Postgres, Elasticsearch, model download, or LLM API is touched.
"""

from __future__ import annotations

import importlib.metadata as metadata
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter

from app.gate.policy import plugin as policy_plugin
from app.gate.policy.gate import PolicyGate, _policy_gate_output
from app.gate.policy.plugin import PLUGIN, PolicyGatePlugin
from app.gate.policy.prompt_registry import YamlPromptRegistry
from app.gates import GATE_GROUP, GateKind, check_gate_plugin
from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.gate import GateOutput, GateVerdict
from core.gate.policy import (
    Citation,
    PolicyGateExtra,
    PolicyGateSnapshot,
    PolicyGateVerdict,
    PromptSnapshot,
)
from core.gate_request import GateRequest
from core.observation import GateContext, ReasonerRegistration
from core.routes import GateRoute
from core.snippet import RetrievedSnippet
from tests._stub_reasoner.plugin import PLUGIN as STUB_PLUGIN

# ---------------------------------------------------------------------------
# The published plugin
# ---------------------------------------------------------------------------


def test_the_policy_gate_plugin_satisfies_the_gate_plugin_protocol():
    assert check_gate_plugin(PLUGIN, name="policy") is PLUGIN
    assert PLUGIN.gate_id == "policy"
    assert PLUGIN.kind == GateKind.LLM
    assert PLUGIN.deterministic is False
    assert PLUGIN.config_model is None


def test_the_interim_root_entry_point_resolves_to_the_policy_gate_plugin():
    # Phase 1a: the framework package publishes the gate it still hosts.
    (entry,) = [
        ep for ep in metadata.entry_points(group=GATE_GROUP) if ep.name == "policy"
    ]
    assert entry.value == "app.gate.policy.plugin:PLUGIN"
    assert entry.load() is PLUGIN


# ---------------------------------------------------------------------------
# build — the gate constructs what it owns
# ---------------------------------------------------------------------------


class _OtherReasonerPlugin:
    """A second reasoner whose prompts directory collides with the stub's."""

    def __init__(self) -> None:
        self.registration = ReasonerRegistration(
            reasoner_id="other-reasoner",
            reasoner_name="Other Reasoner",
            allowed_gate_ids=frozenset({"policy"}),
            allowed_prompt_template_ids=frozenset({"stub-v1"}),
            allowed_jurisdictions=frozenset({"REGION_A"}),
        )
        self.router = APIRouter(prefix="/api/v1/other")
        self.prompts_dir = STUB_PLUGIN.prompts_dir
        self.corpus_dir = STUB_PLUGIN.corpus_dir
        self.raw_event_factory = dict

    def build_services(self, framework: Any) -> Any:
        return MagicMock()


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

    def _register_vector(conn: Any) -> None:
        assert conn is pg_conn
        calls.append("register_vector")

    def _elasticsearch(url: str) -> MagicMock:
        calls.append(f"elasticsearch:{url}")
        return es

    monkeypatch.setattr(policy_plugin.psycopg, "connect", _connect)
    monkeypatch.setattr(policy_plugin, "register_vector", _register_vector)
    monkeypatch.setattr(policy_plugin, "Elasticsearch", _elasticsearch)
    monkeypatch.setattr(policy_plugin, "SentenceTransformer", lambda n: embed_model)
    monkeypatch.setattr(policy_plugin, "CrossEncoder", lambda n: cross_encoder)
    monkeypatch.setattr(policy_plugin, "OpenAILLMClient", lambda: llm_client)
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
        elasticsearch_url="http://es.example:9200",
    )
    framework.reasoners = reasoners
    return framework


def test_build_opens_the_gates_own_connection_index_client_and_models(infra):
    framework = _framework(STUB_PLUGIN)

    gate = PolicyGatePlugin().build(framework)

    assert isinstance(gate, PolicyGate)
    assert infra["calls"] == [
        f"connect:{framework.settings.postgres_dsn}:autocommit=True",
        "register_vector",
        "elasticsearch:http://es.example:9200",
    ]
    assert gate.client is infra["llm_client"]
    retriever = gate.retriever
    assert retriever._pg is infra["pg_conn"]
    assert retriever._es is infra["es"]
    assert retriever._model is infra["embed_model"]
    assert retriever._cross_encoder is infra["cross_encoder"]


def test_build_loads_the_prompt_registry_from_every_composed_reasoner(infra):
    gate = PolicyGatePlugin().build(_framework(STUB_PLUGIN))

    assert isinstance(gate.prompt_registry, YamlPromptRegistry)
    assert gate.prompt_registry.registered_ids() == ["stub-v1"]


def test_two_reasoners_shipping_one_template_id_fail_before_any_connection(infra):
    with pytest.raises(ValueError, match="stub-v1"):
        PolicyGatePlugin().build(_framework(STUB_PLUGIN, _OtherReasonerPlugin()))

    assert infra["calls"] == []


def test_a_failed_build_closes_the_connection_it_opened(infra, monkeypatch):
    def _boom(name: str) -> None:
        raise RuntimeError("model download failed")

    monkeypatch.setattr(policy_plugin, "SentenceTransformer", _boom)

    with pytest.raises(RuntimeError, match="model download failed"):
        PolicyGatePlugin().build(_framework(STUB_PLUGIN))

    infra["pg_conn"].close.assert_called_once()


def test_close_releases_the_index_client_and_the_connection(infra):
    gate = PolicyGatePlugin().build(_framework(STUB_PLUGIN))

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


def _gate(retriever: _RecordingRetriever) -> PolicyGate:
    return PolicyGate(
        client=_ForbiddenClient(),  # type: ignore[arg-type]
        prompt_registry=YamlPromptRegistry([STUB_PLUGIN.prompts_dir]),
        retriever=retriever,  # type: ignore[arg-type]
    )


def _request(observation: Any, **config: Any) -> GateRequest:
    routed = observation.model_copy(
        update={
            "route": GateRoute.ROUTE_TO_GATE,
            "fast_path_rationale": None,
            "gate_context": GateContext(gate_id="policy", gate_config=config),
        }
    )
    return GateRequest(
        decision_id="dec-policy-001",
        observation=routed,
        gate_context=routed.gate_context,
        budget_ms=1_000,
    )


async def test_a_missing_retrieval_query_fails_closed_before_retrieving(observation):
    retriever = _RecordingRetriever()

    output = await _gate(retriever).evaluate(
        _request(observation, template_id="stub-v1", template_vars={})
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


async def test_a_render_error_fails_closed_after_a_tenant_scoped_retrieval(
    observation,
):
    retriever = _RecordingRetriever()

    output = await _gate(retriever).evaluate(
        _request(
            observation,
            template_id="stub-v1",
            # The stub template needs widget_score, thin_history, and region.
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


async def test_an_unknown_template_propagates(observation):
    with pytest.raises(KeyError):
        await _gate(_RecordingRetriever()).evaluate(
            _request(observation, template_id="no-such-template", retrieval_query="q")
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
