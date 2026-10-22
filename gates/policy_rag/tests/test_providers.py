"""The policy gate's retrieval provider seam, driven by a fake provider (DR-34).

No Postgres, Elasticsearch, or model: ``FakeProvider`` (conftest) holds
corpora in memory and can break the evidence contract on demand. These
tests pin:

- ``ProviderRegistry``: providers by id, built on first use (once), closed
  with the gate; an unknown id names what is installed;
- ``discover_provider_plugins``: the gate-owned entry-point group, the
  member's ``reference`` provider, and refusals of a misnamed or duplicate
  plugin;
- ``check_contract``: every answer is a ``ProviderResult`` for the corpus,
  provider, and tier asked, at most ``k`` passages ranked 1..n, none below
  the binding's ``min_provenance``;
- ``retrieve_evidence``: every bound corpus is queried with the binding's
  quota, passages come back in precedence order (tier, then binding order,
  then rank) whatever the binding order, agreeing passages (same text) are
  merged into the higher-precedence one, a corpus's jurisdictions narrow the
  submission's filter (and a corpus they exclude is not queried), and each
  corpus's query is recorded with its provider's state.
"""

from __future__ import annotations

import importlib.metadata as metadata
import threading
from typing import Any

import pytest

from policy_rag_gate.corpus import CorpusRef, PolicyBindingConfig
from policy_rag_gate.gate import (
    SKIPPED,
    check_contract,
    corpus_filter,
    retrieve_evidence,
)
from policy_rag_gate.models import RetrievedContext, content_hash
from policy_rag_gate.providers import (
    PROVIDER_GROUP,
    ProviderContractError,
    ProviderRegistry,
    ProviderResult,
    ProviderServices,
    discover_provider_plugins,
)
from policy_rag_gate.providers.reference import PLUGIN as REFERENCE_PLUGIN
from policy_rag_gate.settings import PolicyRagGateSettings


def _binding(*corpora: dict[str, Any], k: int = 2) -> PolicyBindingConfig:
    return PolicyBindingConfig.model_validate(
        {"corpora": list(corpora), "k_per_corpus": k}
    )


# ---------------------------------------------------------------------------
# ProviderRegistry and discovery
# ---------------------------------------------------------------------------


def test_a_registry_serves_providers_already_built(stub_provider):
    registry = ProviderRegistry.of(stub_provider)

    assert registry.get("reference") is stub_provider
    assert registry.provider_ids() == ["reference"]


def test_an_unknown_provider_names_the_installed_ones(stub_provider):
    registry = ProviderRegistry.of(stub_provider)

    with pytest.raises(LookupError, match=r"'vertex'.*\['reference'\]"):
        registry.get("vertex")


class _CountingPlugin:
    provider_id = "counted"

    def __init__(self, provider: Any, *, fail_first: bool = False) -> None:
        self.builds: list[ProviderServices] = []
        self._provider = provider
        self._fail_first = fail_first

    def build(self, services: ProviderServices) -> Any:
        self.builds.append(services)
        if self._fail_first and len(self.builds) == 1:
            msg = "credentials not mounted yet"
            raise RuntimeError(msg)
        return self._provider


def _services() -> ProviderServices:
    return ProviderServices(
        postgres_dsn="postgresql://u:p@db/d",
        settings=PolicyRagGateSettings(elasticsearch_url="http://es:9200"),
    )


def test_a_provider_is_built_the_first_time_it_is_named_and_only_once(
    fake_provider,
):
    provider = fake_provider({}, provider_id="counted")
    plugin = _CountingPlugin(provider)
    registry = ProviderRegistry({"counted": plugin}, services=_services())

    assert plugin.builds == []  # nothing opened until a binding names it
    assert registry.provider_ids() == ["counted"]
    threads = [
        threading.Thread(target=registry.get, args=("counted",)) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert registry.get("counted") is provider
    assert len(plugin.builds) == 1
    assert plugin.builds[0].postgres_dsn == "postgresql://u:p@db/d"


def test_a_failed_build_propagates_and_is_tried_again(fake_provider):
    provider = fake_provider({}, provider_id="counted")
    plugin = _CountingPlugin(provider, fail_first=True)
    registry = ProviderRegistry({"counted": plugin}, services=_services())

    with pytest.raises(RuntimeError, match="credentials"):
        registry.get("counted")
    assert registry.get("counted") is provider
    assert len(plugin.builds) == 2


def test_closing_the_registry_closes_what_it_serves(fake_provider):
    built = fake_provider({}, provider_id="counted")
    unused = fake_provider({}, provider_id="other")
    registry = ProviderRegistry(
        {"counted": _CountingPlugin(built)}, services=_services(), providers=[unused]
    )
    registry.get("counted")

    registry.close()

    assert built.closed is True
    assert unused.closed is True


def test_plugins_need_services_to_be_built(fake_provider):
    with pytest.raises(ValueError, match="ProviderServices"):
        ProviderRegistry({"counted": _CountingPlugin(fake_provider({}))})


def test_a_provider_may_not_be_both_built_and_a_plugin(fake_provider):
    with pytest.raises(ValueError, match="both built and as plugins"):
        ProviderRegistry(
            {"counted": _CountingPlugin(fake_provider({}))},
            services=_services(),
            providers=[fake_provider({}, provider_id="counted")],
        )


def test_the_member_publishes_the_reference_provider_in_its_own_group():
    (entry,) = [
        ep
        for ep in metadata.entry_points(group=PROVIDER_GROUP)
        if ep.name == "reference"
    ]
    assert entry.value == "policy_rag_gate.providers.reference:PLUGIN"
    assert entry.dist is not None
    assert entry.dist.name == "decisionledger-gate-policy-rag"
    assert discover_provider_plugins()["reference"] is REFERENCE_PLUGIN


class _EntryPoint:
    def __init__(self, name: str, target: Any) -> None:
        self.name = name
        self._target = target

    def load(self) -> Any:
        return self._target


def test_discovery_refuses_a_plugin_whose_id_is_not_its_entry_point_name():
    with pytest.raises(ValueError, match=r"'vertex'.*'reference'"):
        discover_provider_plugins([_EntryPoint("vertex", REFERENCE_PLUGIN)])  # type: ignore[list-item]


def test_discovery_refuses_two_plugins_of_one_name():
    with pytest.raises(ValueError, match=r"two .* named 'reference'"):
        discover_provider_plugins(
            [
                _EntryPoint("reference", REFERENCE_PLUGIN),  # type: ignore[list-item]
                _EntryPoint("reference", REFERENCE_PLUGIN),  # type: ignore[list-item]
            ]
        )


# ---------------------------------------------------------------------------
# check_contract — the evidence contract every provider answer meets
# ---------------------------------------------------------------------------

_TIER_1 = CorpusRef(corpus_id="stub-policy", tier=1)


def _answer(stub_provider: Any, corpus: CorpusRef = _TIER_1, k: int = 2) -> Any:
    return stub_provider.retrieve("q", corpus, k=k, jurisdictions=None, risk_tier=None)


def test_a_conforming_answer_passes_as_it_is(stub_provider):
    answer = _answer(stub_provider)

    assert check_contract(_TIER_1, answer, k=2) is answer


@pytest.mark.parametrize(
    "answer",
    [
        [{"text": "a passage"}],
        {"state": {}, "contexts": [], "ranking_requested": "x", "ranking_applied": "x"},
        None,
    ],
)
def test_an_answer_that_is_not_a_provider_result_breaks_the_contract(answer):
    with pytest.raises(ProviderContractError, match="not a ProviderResult"):
        check_contract(_TIER_1, answer, k=2)


def test_a_passage_whose_digest_is_not_its_texts_is_refused_by_the_model(
    stub_provider,
):
    context = _answer(stub_provider).contexts[0]

    with pytest.raises(ValueError, match="SHA-256"):
        RetrievedContext.model_validate(
            {**context.model_dump(), "content_hash": content_hash("other text")}
        )


def test_a_result_built_without_validation_cannot_misstate_a_digest(stub_provider):
    # ``model_construct`` skips the model's own check; the gate re-checks.
    answer = _answer(stub_provider)
    forged = RetrievedContext.model_construct(
        **{**dict(answer.contexts[0]), "content_hash": content_hash("other text")}
    )
    result = ProviderResult.model_construct(
        **{**dict(answer), "contexts": (forged, *answer.contexts[1:])}
    )

    with pytest.raises(ProviderContractError, match="not the SHA-256 of its text"):
        check_contract(_TIER_1, result, k=2)


def test_a_passage_that_is_not_a_retrieved_context_breaks_the_contract(
    stub_provider,
):
    answer = _answer(stub_provider)
    result = ProviderResult.model_construct(
        **{**dict(answer), "contexts": ({"text": "a passage"},)}
    )

    with pytest.raises(ProviderContractError, match="not a RetrievedContext"):
        check_contract(_TIER_1, result, k=2)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("corpus_id", "other-corpus", "labeled reference/other-corpus"),
        ("provider", "vertex", "labeled vertex/stub-policy"),
        ("tier", 2, "at tier 2"),
    ],
)
def test_a_passage_labeled_for_another_corpus_breaks_the_contract(
    fake_provider, fake_passage, field, value, match
):
    provider = fake_provider(
        {"stub-policy": [fake_passage("DOC-A", "a")]},
        tamper=lambda result: result.model_copy(
            update={
                "contexts": tuple(
                    c.model_copy(update={field: value}) for c in result.contexts
                )
            }
        ),
    )

    with pytest.raises(ProviderContractError, match=match):
        check_contract(_TIER_1, _answer(provider), k=2)


def test_more_passages_than_the_quota_break_the_contract(stub_provider):
    with pytest.raises(ProviderContractError, match="2 passages for a quota of 1"):
        check_contract(_TIER_1, _answer(stub_provider, k=2), k=1)


def test_passages_out_of_rank_order_break_the_contract(fake_provider, fake_passage):
    provider = fake_provider(
        {"stub-policy": [fake_passage("DOC-A", "a"), fake_passage("DOC-B", "b")]},
        tamper=lambda result: result.model_copy(
            update={"contexts": tuple(reversed(result.contexts))}
        ),
    )

    with pytest.raises(ProviderContractError, match=r"ranked its passages \[2, 1\]"):
        check_contract(_TIER_1, _answer(provider), k=2)


def test_a_passage_below_the_bindings_min_provenance_breaks_the_contract(
    fake_provider, fake_passage
):
    provider = fake_provider(
        {"stub-policy": [fake_passage("DOC-A", "a")]}, passage_provenance="text_only"
    )
    strict = CorpusRef(corpus_id="stub-policy", tier=1, min_provenance="uri_and_hash")

    with pytest.raises(ProviderContractError, match="'text_only' provenance"):
        check_contract(strict, _answer(provider, strict), k=2)


def test_a_state_reported_for_another_corpus_breaks_the_contract(
    fake_provider, fake_passage
):
    provider = fake_provider(
        {"stub-policy": [fake_passage("DOC-A", "a")]},
        tamper=lambda result: result.model_copy(
            update={"state": result.state.model_copy(update={"corpus_id": "x"})}
        ),
    )

    with pytest.raises(ProviderContractError, match="reported the state of"):
        check_contract(_TIER_1, _answer(provider), k=2)


# ---------------------------------------------------------------------------
# retrieve_evidence — fan-out, precedence, merging, filters
# ---------------------------------------------------------------------------


@pytest.fixture
def two_corpora(fake_provider, fake_passage) -> Any:
    """A regulatory corpus and a practice-notes corpus; they share one passage."""
    return fake_provider(
        {
            "regulatory": [
                fake_passage("REG-1", "Hold any account with a fresh device."),
                fake_passage("REG-2", "Shared text both corpora carry."),
            ],
            "practice-notes": [
                fake_passage("PN-1", "Challenge a fresh device with step-up."),
                fake_passage("PN-2", "Shared text both corpora carry."),
            ],
        }
    )


def test_every_bound_corpus_is_queried_with_the_bindings_quota(two_corpora):
    binding = _binding(
        {"corpus_id": "regulatory", "tier": 1},
        {"corpus_id": "practice-notes", "tier": 2},
        k=3,
    )

    evidence = retrieve_evidence(
        ProviderRegistry.of(two_corpora), binding, "fresh device"
    )

    assert [(c["corpus_id"], c["tier"], c["k"]) for c in two_corpora.calls] == [
        ("regulatory", 1, 3),
        ("practice-notes", 2, 3),
    ]
    assert [q.corpus_ref.corpus_id for q in evidence.queries] == [
        "regulatory",
        "practice-notes",
    ]
    assert all(q.k == 3 for q in evidence.queries)


def test_two_corpora_at_two_tiers_render_highest_tier_first_whatever_the_binding_order(
    two_corpora,
):
    # The practice notes are bound first, but the regulatory corpus is tier 1.
    binding = _binding(
        {"corpus_id": "practice-notes", "tier": 2},
        {"corpus_id": "regulatory", "tier": 1},
    )

    evidence = retrieve_evidence(ProviderRegistry.of(two_corpora), binding, "q")

    assert [(c.corpus_id, c.tier, c.document_id) for c in evidence.contexts] == [
        ("regulatory", 1, "REG-1"),
        ("regulatory", 1, "REG-2"),
        ("practice-notes", 2, "PN-1"),
    ]
    # The queries stay in binding order, as the binding names them.
    assert [q.corpus_ref.tier for q in evidence.queries] == [2, 1]


def test_an_agreeing_passage_is_merged_into_the_higher_tier_one(two_corpora):
    binding = _binding(
        {"corpus_id": "practice-notes", "tier": 2},
        {"corpus_id": "regulatory", "tier": 1},
    )

    evidence = retrieve_evidence(ProviderRegistry.of(two_corpora), binding, "q")

    shared = content_hash("Shared text both corpora carry.")
    assert [c.corpus_id for c in evidence.contexts if c.content_hash == shared] == [
        "regulatory"
    ]
    assert evidence.dedup_merged == 1
    merged = {q.corpus_ref.corpus_id: (q.returned, q.merged) for q in evidence.queries}
    assert merged == {"practice-notes": (2, 1), "regulatory": (2, 0)}
    # Each corpus's own answer is kept whole, before merging.
    assert len(evidence.results["practice-notes"].contexts) == 2  # type: ignore[union-attr]


def test_corpora_of_one_tier_keep_the_binding_order(fake_provider, fake_passage):
    provider = fake_provider(
        {
            "a": [fake_passage("A-1", "a one"), fake_passage("A-2", "a two")],
            "b": [fake_passage("B-1", "b one")],
        }
    )
    binding = _binding({"corpus_id": "b", "tier": 1}, {"corpus_id": "a", "tier": 1})

    evidence = retrieve_evidence(ProviderRegistry.of(provider), binding, "q")

    assert [c.document_id for c in evidence.contexts] == ["B-1", "A-1", "A-2"]


def test_the_same_text_under_another_jurisdiction_is_not_merged(
    fake_provider, fake_passage
):
    # Two corpora carry one paragraph, each in its own jurisdiction's
    # document: both passages are evidence, and both reach enforcement.
    provider = fake_provider(
        {
            "us-policy": [fake_passage("US-1", "Boilerplate.", jurisdiction="US")],
            "eu-policy": [fake_passage("EU-1", "Boilerplate.", jurisdiction="EU")],
        }
    )
    binding = _binding(
        {"corpus_id": "us-policy", "tier": 1}, {"corpus_id": "eu-policy", "tier": 1}
    )

    evidence = retrieve_evidence(ProviderRegistry.of(provider), binding, "q")

    assert [(c.document_id, c.jurisdiction) for c in evidence.contexts] == [
        ("US-1", "US"),
        ("EU-1", "EU"),
    ]
    assert evidence.dedup_merged == 0


def test_identical_text_within_one_corpus_is_rendered_once(fake_provider, fake_passage):
    provider = fake_provider(
        {"a": [fake_passage("A-1", "same"), fake_passage("A-2", "same")]}
    )

    evidence = retrieve_evidence(
        ProviderRegistry.of(provider), _binding({"corpus_id": "a", "tier": 1}), "q"
    )

    assert [c.document_id for c in evidence.contexts] == ["A-1"]
    assert evidence.dedup_merged == 1


def test_corpora_held_by_different_providers_are_each_asked(
    fake_provider, fake_passage
):
    reference = fake_provider({"local": [fake_passage("L-1", "local text")]})
    cloud = fake_provider(
        {"cloud-corpus": [fake_passage("C-1", "cloud text")]},
        provider_id="cloud",
        provenance_level="uri_and_hash",
    )
    binding = _binding(
        {"corpus_id": "cloud-corpus", "provider": "cloud", "tier": 1},
        {"corpus_id": "local", "tier": 2},
    )

    evidence = retrieve_evidence(ProviderRegistry.of(reference, cloud), binding, "q")

    assert [(c.provider, c.corpus_id) for c in evidence.contexts] == [
        ("cloud", "cloud-corpus"),
        ("reference", "local"),
    ]
    assert [q.state.provenance_level for q in evidence.queries if q.state] == [
        "uri_and_hash",
        "pinned_version",
    ]


def test_each_query_records_the_corpus_state_and_ranking(fake_provider, fake_passage):
    provider = fake_provider(
        {"a": [fake_passage("A-1", "text")]},
        ranking_requested="reranked",
        ranking_applied="rrf_only",
    )

    evidence = retrieve_evidence(
        ProviderRegistry.of(provider),
        _binding({"corpus_id": "a", "tier": 1}),
        "q",
        jurisdictions=["REGION_A"],
        risk_tier="STANDARD",
    )

    (query,) = evidence.queries
    assert query.state is not None
    assert query.state.state_marker == "fake:a:1"
    assert (query.ranking_requested, query.ranking_applied) == ("reranked", "rrf_only")
    assert query.filters.jurisdictions == ("REGION_A",)
    assert query.filters.risk_tier == "STANDARD"
    assert query.returned == 1
    assert query.skipped is None
    assert provider.calls[0]["risk_tier"] == "STANDARD"


def test_no_jurisdiction_filter_reaches_the_provider_as_none(
    stub_provider, stub_binding
):
    retrieve_evidence(ProviderRegistry.of(stub_provider), stub_binding, "q")

    assert stub_provider.calls[0]["jurisdictions"] is None


@pytest.mark.parametrize(
    ("bound", "submitted", "expected"),
    [
        ((), ["REGION_A", "REGION_B"], (["REGION_A", "REGION_B"], None)),
        ((), [], (None, None)),
        (("REGION_B",), [], (["REGION_B"], None)),
        (("REGION_A", "REGION_B"), ["REGION_B", "REGION_C"], (["REGION_B"], None)),
    ],
)
def test_a_corpus_jurisdictions_narrow_the_submissions_filter(
    bound, submitted, expected
):
    corpus = CorpusRef(corpus_id="a", tier=1, jurisdictions=frozenset(bound))

    assert corpus_filter(corpus, submitted) == expected


def test_a_corpus_the_submissions_filter_excludes_is_not_queried(two_corpora):
    binding = _binding(
        {"corpus_id": "regulatory", "tier": 1, "jurisdictions": ["REGION_B"]},
        {"corpus_id": "practice-notes", "tier": 2},
    )

    evidence = retrieve_evidence(
        ProviderRegistry.of(two_corpora), binding, "q", jurisdictions=["REGION_A"]
    )

    assert [c["corpus_id"] for c in two_corpora.calls] == ["practice-notes"]
    skipped = evidence.queries[0]
    assert skipped.state is None
    assert skipped.ranking_applied == SKIPPED
    assert skipped.returned == 0
    assert skipped.skipped is not None
    assert "REGION_B" in skipped.skipped
    assert evidence.results["regulatory"] is None
    assert {c.corpus_id for c in evidence.contexts} == {"practice-notes"}


def test_a_provider_error_propagates(fake_provider, stub_binding):
    provider = fake_provider({}, retrieve_error=ConnectionError("index down"))

    with pytest.raises(ConnectionError, match="index down"):
        retrieve_evidence(ProviderRegistry.of(provider), stub_binding, "q")


def test_a_provider_that_is_not_installed_is_a_lookup_error(stub_provider):
    binding = _binding({"corpus_id": "x", "provider": "vertex", "tier": 1})

    with pytest.raises(LookupError, match="'vertex'"):
        retrieve_evidence(ProviderRegistry.of(stub_provider), binding, "q")


def test_retrieved_passages_are_the_contexts_the_provider_returned(stub_provider):
    evidence = retrieve_evidence(
        ProviderRegistry.of(stub_provider),
        _binding({"corpus_id": "stub-policy", "tier": 1}, k=5),
        "calibration",
    )

    assert all(isinstance(c, RetrievedContext) for c in evidence.contexts)
    assert [c.rank for c in evidence.contexts] == [1, 2]
    assert evidence.contexts[0].content_hash == content_hash(evidence.contexts[0].text)
