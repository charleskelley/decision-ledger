"""Corpus references and the policy gate's binding configuration (DR-34).

``PolicyBindingConfig`` is the gate plugin's ``binding_config_model``: the
corpora a reasoner's binding names, each a ``CorpusRef`` with an authority
tier and a minimum provenance, and the quota per corpus. It validates the
binding's JSON as it arrives from a registration file or a ``PUT``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from policy_rag_gate.corpus import (
    DEFAULT_K_PER_CORPUS,
    MAX_K_PER_CORPUS,
    PROVENANCE_ORDER,
    CorpusRef,
    CorpusState,
    PolicyBindingConfig,
    provenance_at_least,
)
from policy_rag_gate.plugin import PLUGIN


def test_the_plugin_validates_bindings_with_the_policy_binding_config():
    assert PLUGIN.binding_config_model is PolicyBindingConfig


def test_a_corpus_reference_defaults_to_the_reference_provider_and_text_only():
    ref = CorpusRef.model_validate({"corpus_id": "ato-policy", "tier": 1})

    assert ref.provider == "reference"
    assert ref.resource is None
    assert ref.jurisdictions == frozenset()
    assert ref.min_provenance == "text_only"


def test_a_corpus_reference_takes_the_json_it_arrives_as():
    ref = CorpusRef.model_validate(
        {
            "corpus_id": "regulatory",
            "provider": "vertex",
            "resource": "projects/p/locations/l/ragCorpora/123",
            "tier": 1,
            "jurisdictions": ["EU", "UK", "EU"],
            "min_provenance": "uri_and_hash",
        }
    )

    assert ref.jurisdictions == frozenset({"EU", "UK"})
    assert ref.resource == "projects/p/locations/l/ragCorpora/123"


@pytest.mark.parametrize("tier", [0, -1, True, "1", 1.0, None])
def test_a_tier_is_a_json_integer_of_one_or_more(tier):
    with pytest.raises(ValidationError):
        CorpusRef.model_validate({"corpus_id": "a", "tier": tier})


def test_a_corpus_reference_needs_its_tier():
    with pytest.raises(ValidationError, match="tier"):
        CorpusRef.model_validate({"corpus_id": "a"})


@pytest.mark.parametrize(
    "corpus_id", ["", "-starts-with-dash", "has/slash", "has space", "x" * 129]
)
def test_a_corpus_id_is_an_identifier(corpus_id):
    with pytest.raises(ValidationError):
        CorpusRef.model_validate({"corpus_id": corpus_id, "tier": 1})


def test_a_corpus_reference_refuses_unknown_keys_and_levels():
    with pytest.raises(ValidationError):
        CorpusRef.model_validate({"corpus_id": "a", "tier": 1, "weight": 2})
    with pytest.raises(ValidationError):
        CorpusRef.model_validate({"corpus_id": "a", "tier": 1, "min_provenance": "x"})
    with pytest.raises(ValidationError):
        CorpusRef.model_validate({"corpus_id": "a", "tier": 1, "jurisdictions": [""]})


def test_a_binding_names_at_least_one_corpus():
    with pytest.raises(ValidationError, match="corpora"):
        PolicyBindingConfig.model_validate({})
    with pytest.raises(ValidationError, match="corpora"):
        PolicyBindingConfig.model_validate({"corpora": []})


def test_a_binding_names_each_corpus_once():
    with pytest.raises(ValidationError, match=r"\['a'\] are bound more than once"):
        PolicyBindingConfig.model_validate(
            {
                "corpora": [
                    {"corpus_id": "a", "tier": 1},
                    {"corpus_id": "a", "tier": 2},
                ]
            }
        )


def test_two_corpora_may_share_a_tier_and_the_binding_reports_its_tiers():
    binding = PolicyBindingConfig.model_validate(
        {
            "corpora": [
                {"corpus_id": "notes", "tier": 2},
                {"corpus_id": "law", "tier": 1},
                {"corpus_id": "faq", "tier": 2},
            ]
        }
    )

    assert binding.tiers == (1, 2)
    assert binding.k_per_corpus == DEFAULT_K_PER_CORPUS


@pytest.mark.parametrize("k", [0, MAX_K_PER_CORPUS + 1, True, "5", 5.0])
def test_the_quota_per_corpus_is_a_bounded_integer(k):
    with pytest.raises(ValidationError):
        PolicyBindingConfig.model_validate(
            {"corpora": [{"corpus_id": "a", "tier": 1}], "k_per_corpus": k}
        )


def test_a_binding_survives_the_registration_json_round_trip():
    binding = PolicyBindingConfig.model_validate(
        {
            "corpora": [
                {
                    "corpus_id": "law",
                    "tier": 1,
                    "jurisdictions": ["EU"],
                    "min_provenance": "pinned_version",
                },
                {"corpus_id": "notes", "tier": 2},
            ],
            "k_per_corpus": 3,
        }
    )

    assert PolicyBindingConfig.model_validate(binding.model_dump(mode="json")) == (
        binding
    )


def test_a_binding_refuses_unknown_keys():
    with pytest.raises(ValidationError):
        PolicyBindingConfig.model_validate(
            {"corpora": [{"corpus_id": "a", "tier": 1}], "top_k": 5}
        )


def test_provenance_levels_are_ordered_weakest_first():
    assert PROVENANCE_ORDER == ("text_only", "uri_and_hash", "pinned_version")
    assert provenance_at_least("pinned_version", "text_only")
    assert provenance_at_least("uri_and_hash", "uri_and_hash")
    assert not provenance_at_least("text_only", "uri_and_hash")
    assert not provenance_at_least("uri_and_hash", "pinned_version")


def test_a_corpus_state_reloads_from_its_stored_json():
    state = CorpusState(
        corpus_id="law",
        provider="reference",
        provider_version="reference-1",
        state_marker="abc",
        provenance_level="pinned_version",
        document_count=3,
        updated_at=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
    )

    assert CorpusState.model_validate(state.model_dump(mode="json"), strict=False) == (
        state
    )


def test_a_references_jurisdictions_serialize_sorted_in_every_process():
    # A frozenset iterates in hash order, which varies between processes;
    # the snapshot that records the reference must not.
    corpus = CorpusRef(
        corpus_id="regulatory", tier=1, jurisdictions=frozenset({"US", "EU", "UK"})
    )

    assert corpus.model_dump(mode="json")["jurisdictions"] == ["EU", "UK", "US"]
    assert '"jurisdictions":["EU","UK","US"]' in corpus.model_dump_json()
    assert CorpusRef.model_validate_json(corpus.model_dump_json()) == corpus
