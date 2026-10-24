"""The generated registration file: one registration however the ledger is told.

``registration.yaml`` is generated from ``registry.py`` (``python -m
mock_batch_reasoner registration-yaml``), so a ledger that registers the mock
by file, by a PUT of the same entry as JSON, or by its plugin serves one
``registration_version``. The committed file must be what the module
generates now: a change to the registration that is not regenerated fails
here.
"""

from __future__ import annotations

import json

import yaml

from app.api.registrations import parse_registration
from app.plugins import load_registration_file
from core.observation import canonical_registration, registration_version
from mock_batch_reasoner.registry import MOCK_BATCH_REGISTRATION, registration_yaml

VERSION = registration_version(MOCK_BATCH_REGISTRATION)


def test_the_committed_registration_file_is_what_the_registry_generates(
    registration_file,
):
    assert registration_file.read_bytes() == registration_yaml().encode("utf-8"), (
        "registration.yaml is stale: regenerate it with `make registration-mock_batch`"
    )


def test_a_ledger_loads_the_file_to_the_registration_with_the_same_version(
    registration_file,
):
    [registration] = load_registration_file(registration_file)

    assert registration == MOCK_BATCH_REGISTRATION
    assert registration_version(registration) == VERSION


def test_the_put_body_is_the_files_entry_as_json_and_validates_to_the_same_version(
    registration_file,
):
    file = yaml.safe_load(registration_file.read_text(encoding="utf-8"))
    [entry] = file["registrations"]
    body = json.dumps(canonical_registration(MOCK_BATCH_REGISTRATION))

    assert json.loads(body) == entry
    registration = parse_registration(MOCK_BATCH_REGISTRATION.reasoner_id, body)
    assert registration == MOCK_BATCH_REGISTRATION
    assert registration_version(registration) == VERSION
