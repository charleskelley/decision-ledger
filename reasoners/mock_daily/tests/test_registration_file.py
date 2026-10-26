"""The generated registration file: one registration however the ledger is told.

``registration.yaml`` is generated from ``registry.py`` (``python -m
mock_daily_reasoner registration-yaml``), so a ledger that registers the mock
by file, by a PUT of the same entry as JSON, or by its plugin serves one
``registration_version``. The committed file must be what the module
generates now: a change to the registration that is not regenerated fails
here. The registration binds the rules gate mandatory, so a ledger that does
not compose the gate refuses the mock however it is registered, at startup
or at the PUT; one that composes it admits the PUT at the same version.
"""

from __future__ import annotations

import json

import pytest
import yaml

from app.api.registrations import parse_registration
from app.auth import OPERATOR
from app.client import LocalClient
from app.plugins import load_registration_file
from core.exceptions import GatePluginError, LedgerError
from core.observation import canonical_registration, registration_version
from mock_daily_reasoner.eval.driver import rules_gate_plugin
from mock_daily_reasoner.plugin import PLUGIN
from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION, registration_yaml
from tests._memory_ledger import memory_framework

VERSION = registration_version(MOCK_DAILY_REGISTRATION)


def test_the_committed_registration_file_is_what_the_registry_generates(
    registration_file,
):
    assert registration_file.read_bytes() == registration_yaml().encode("utf-8"), (
        "registration.yaml is stale: regenerate it with `make registration-mock_daily`"
    )


def test_a_ledger_loads_the_file_to_the_registration_with_the_same_version(
    registration_file,
):
    [registration] = load_registration_file(registration_file)

    assert registration == MOCK_DAILY_REGISTRATION
    assert registration_version(registration) == VERSION


def test_the_put_body_is_the_files_entry_as_json_and_validates_to_the_same_version(
    registration_file,
):
    file = yaml.safe_load(registration_file.read_text(encoding="utf-8"))
    [entry] = file["registrations"]
    body = json.dumps(canonical_registration(MOCK_DAILY_REGISTRATION))

    assert json.loads(body) == entry
    registration = parse_registration(MOCK_DAILY_REGISTRATION.reasoner_id, body)
    assert registration == MOCK_DAILY_REGISTRATION
    assert registration_version(registration) == VERSION


def test_a_ledger_without_the_rules_gate_refuses_to_start_with_the_mock(
    registration_file,
):
    with pytest.raises(GatePluginError, match="binds this gate as mandatory"):
        memory_framework([PLUGIN], [])
    with pytest.raises(GatePluginError, match="binds this gate as mandatory"):
        memory_framework((), [], registration_file=registration_file)


async def test_a_put_of_the_registration_is_admitted_only_with_the_rules_gate():
    bare = LocalClient(memory_framework(), principal=OPERATOR)
    ledger = memory_framework((), [rules_gate_plugin()])

    with pytest.raises(LedgerError) as refused:
        await bare.register(MOCK_DAILY_REGISTRATION)
    view = await LocalClient(ledger, principal=OPERATOR).register(
        MOCK_DAILY_REGISTRATION
    )

    assert (refused.value.status, refused.value.code) == (422, "GATE_BINDING_REFUSED")
    assert (view.registration_version, view.source) == (VERSION, "api")
    assert ledger.registry.get(MOCK_DAILY_REGISTRATION.reasoner_id) == (
        MOCK_DAILY_REGISTRATION
    )
