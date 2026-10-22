"""RetrievedContext — the evidence contract every retrieval provider meets (DR-34).

A provider cannot hand the gate a passage without its corpus, tier,
document, rank, provenance level, and the SHA-256 of its text, and the
digest must be the text's. The model is strict and frozen, refuses unknown
keys and non-finite scores, and survives the bundle's JSON round trip.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from policy_rag_gate.models import RetrievedContext, content_hash


def _fields(**overrides: Any) -> dict[str, Any]:
    text = overrides.pop("text", "Widgets with a thin history SHALL be held.")
    fields: dict[str, Any] = {
        "corpus_id": "stub-policy",
        "provider": "reference",
        "tier": 1,
        "document_id": "STUB-POLICY-A",
        "title": "Stub Widget Policy A",
        "version": "1.0",
        "jurisdiction": "REGION_A",
        "section_path": "2. Calibration Review",
        "span": (10, 10 + len(text)),
        "text": text,
        "content_hash": content_hash(text),
        "rank": 1,
        "scores": {"rerank": -4.32},
        "provider_chunk_id": "c" * 64,
        "provenance_level": "pinned_version",
        "document_metadata": {
            "approver": "risk-office",
            "effective_date": "2024-01-01",
        },
    }
    fields.update(overrides)
    return fields


def test_the_digest_is_the_sha256_of_the_utf8_text():
    text = "Résumé — §3.2 applies."
    assert content_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_passage_carries_its_corpus_tier_document_and_provenance():
    context = RetrievedContext(**_fields())

    assert (context.corpus_id, context.tier, context.document_id) == (
        "stub-policy",
        1,
        "STUB-POLICY-A",
    )
    assert context.provenance_level == "pinned_version"
    assert context.prompted_text is None
    assert context.artifact_hash is None


def test_a_digest_that_is_not_the_texts_is_refused():
    with pytest.raises(ValidationError, match="not the SHA-256 of its text"):
        RetrievedContext(**_fields(content_hash=content_hash("another passage")))


@pytest.mark.parametrize("digest", ["", "ABC", "0" * 63, "sha256:" + "0" * 64])
def test_a_digest_that_is_not_sha256_hex_is_refused(digest):
    with pytest.raises(ValidationError):
        RetrievedContext(**_fields(content_hash=digest))


@pytest.mark.parametrize("missing", ["corpus_id", "tier", "content_hash", "rank"])
def test_the_contract_fields_are_required(missing):
    fields = _fields()
    del fields[missing]

    with pytest.raises(ValidationError, match=missing):
        RetrievedContext(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tier": 0},
        {"rank": 0},
        {"provenance_level": "best_effort"},
        {"span": (5, 2)},
        {"span": (-1, 4)},
        {"scores": {"rerank": float("nan")}},
        {"surprise": True},
    ],
    ids=["tier", "rank", "level", "span-order", "span-negative", "nan", "extra"],
)
def test_a_malformed_passage_is_refused(overrides):
    with pytest.raises(ValidationError):
        RetrievedContext(**_fields(**overrides))


def test_unknown_details_are_optional():
    context = RetrievedContext(
        **_fields(
            version=None,
            jurisdiction=None,
            section_path=None,
            span=None,
            document_uri=None,
            provider_chunk_id=None,
            document_metadata={},
            scores={},
        )
    )

    assert context.version is None
    assert context.span is None


def test_a_passage_is_immutable():
    context = RetrievedContext(**_fields())
    with pytest.raises(ValidationError):
        context.text = "tampered"  # type: ignore[misc]


def test_a_passage_survives_the_bundles_json_round_trip():
    context = RetrievedContext(**_fields())

    stored = json.loads(json.dumps(context.model_dump(mode="json")))

    assert RetrievedContext.model_validate(stored, strict=False) == context
