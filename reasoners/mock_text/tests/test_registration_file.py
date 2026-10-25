"""The generated registration file, and the registration it carries.

``registration.yaml`` is generated from ``registry.py`` (``python -m
mock_text_reasoner registration-yaml``), so a ledger that registers the mock
by file, by a PUT of the same entry as JSON, or by its plugin serves one
``registration_version``. The committed file must be what the module
generates now: a change to the registration that is not regenerated fails
here. The registration binds the RAG policy gate, advisory, to two corpora
of different authority (the regulatory text at tier 1, the practice notes at
tier 2), records nothing (no bypass rule, no recordable action), names its
customers in one native namespace, and declares no novel-entity feature and
no jurisdiction conflict group.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

import yaml

from app.api.registrations import parse_registration
from app.plugins import load_registration_file
from core.observation import canonical_registration, registration_version
from mock_text_reasoner.registry import (
    MOCK_TEXT_CUSTOMER_NAMESPACE,
    MOCK_TEXT_PRACTICE_CORPUS_ID,
    MOCK_TEXT_PRACTICE_TIER,
    MOCK_TEXT_REGISTRATION,
    MOCK_TEXT_REGULATORY_CORPUS_ID,
    MOCK_TEXT_REGULATORY_TIER,
    POLICY_GATE_BINDING,
    registration_yaml,
)

if TYPE_CHECKING:
    from pathlib import Path

VERSION = registration_version(MOCK_TEXT_REGISTRATION)


def test_the_committed_registration_file_is_what_the_registry_generates(
    registration_file: Path,
):
    assert registration_file.read_bytes() == registration_yaml().encode("utf-8"), (
        "registration.yaml is stale: regenerate it with `make registration-mock_text`"
    )


def test_a_ledger_loads_the_file_to_the_registration_with_the_same_version(
    registration_file: Path,
):
    [registration] = load_registration_file(registration_file)

    assert registration == MOCK_TEXT_REGISTRATION
    assert registration_version(registration) == VERSION


def test_the_put_body_is_the_files_entry_as_json_and_validates_to_the_same_version(
    registration_file: Path,
):
    file = yaml.safe_load(registration_file.read_text(encoding="utf-8"))
    [entry] = file["registrations"]
    body = json.dumps(canonical_registration(MOCK_TEXT_REGISTRATION))

    assert json.loads(body) == entry
    registration = parse_registration(MOCK_TEXT_REGISTRATION.reasoner_id, body)
    assert registration == MOCK_TEXT_REGISTRATION
    assert registration_version(registration) == VERSION


def test_the_registration_binds_the_policy_gate_advisory_to_two_corpora_by_tier():
    registration = MOCK_TEXT_REGISTRATION

    [binding] = registration.gates
    assert binding == POLICY_GATE_BINDING
    assert registration.gate_ids == frozenset({"policy"})
    assert (binding.gate_id, binding.mandatory) == ("policy", False)
    assert binding.config == {
        "corpora": [
            {
                "corpus_id": "mock-text-regulatory",
                "provider": "reference",
                "tier": 1,
                "min_provenance": "pinned_version",
            },
            {
                "corpus_id": "mock-text-practice",
                "provider": "reference",
                "tier": 2,
                "min_provenance": "pinned_version",
            },
        ],
        "k_per_corpus": 5,
    }
    assert (MOCK_TEXT_REGULATORY_CORPUS_ID, MOCK_TEXT_REGULATORY_TIER) == (
        "mock-text-regulatory",
        1,
    )
    assert (MOCK_TEXT_PRACTICE_CORPUS_ID, MOCK_TEXT_PRACTICE_TIER) == (
        "mock-text-practice",
        2,
    )


def test_the_registration_records_nothing():
    registration = MOCK_TEXT_REGISTRATION

    assert registration.bypass_rules == ()
    assert registration.recordable_actions == frozenset()


def test_the_registration_names_its_customers_and_no_novel_entity_feature():
    registration = MOCK_TEXT_REGISTRATION

    assert registration.native_namespaces == (MOCK_TEXT_CUSTOMER_NAMESPACE,)
    namespace = registration.native_namespace("mock-text-customer")
    assert namespace is not None
    assert namespace.entity_type == "customer"
    assert namespace.namespace_uuid == UUID("a2f8a070-abbb-4956-abdb-127755ca4b30")
    assert registration.novel_entity_feature is None


def test_the_registration_declares_no_jurisdiction_conflict_group():
    # A protocol hold is the policy gate's verdict, never a jurisdiction
    # conflict: no group may span the corpora's jurisdictions (Tier 5).
    assert MOCK_TEXT_REGISTRATION.jurisdiction_conflict_groups == ()
