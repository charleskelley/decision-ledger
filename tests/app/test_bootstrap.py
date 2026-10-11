"""Behavioral tests for ``app.bootstrap.build_framework_services``.

``build_framework_services`` is the single place the framework wires its
shared services from a ``FrameworkSettings`` and the composed plugins. Every
lifespan test replaces it with a fake, so without this module the wiring
itself — which plugin fields feed which service — has no unit coverage.

Infrastructure clients and models are replaced with recording fakes at the
``app.bootstrap`` module boundary. No Postgres, Elasticsearch, model download,
or OpenAI client is touched. What is asserted is the composition:

- the canonical DDL is applied before pgvector adapters are registered (the
  DDL creates the ``vector`` type that ``register_vector`` looks up);
- the prompt registry is built from every plugin's ``prompts_dir``;
- the bundle store receives one raw-event factory per ``reasoner_id`` and the
  registry built from the plugins' registrations;
- two plugins shipping the same ``template_id`` fail bootstrap loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter

from app import bootstrap
from app.audit.store import BundleStore
from app.gate.policy.gate import PolicyGate
from app.plugin import FrameworkServices
from app.retrieval.retriever import PolicyRetriever
from app.settings import FrameworkSettings
from core.observation import ReasonerRegistration
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.observation import ReasonerRegistry


class _RecordingStore(BundleStore):
    """``BundleStore`` that remembers the constructor arguments it received."""

    last_factories: Mapping[str, Any] | None = None
    last_registry: ReasonerRegistry | None = None

    def __init__(self, conn: Any, *, raw_event_factories: Any, registry: Any) -> None:
        super().__init__(
            conn, raw_event_factories=raw_event_factories, registry=registry
        )
        _RecordingStore.last_factories = raw_event_factories
        _RecordingStore.last_registry = registry


class _OtherPlugin:
    """A second plugin whose prompts directory collides with the stub's."""

    def __init__(self, prompts_dir: Path) -> None:
        self.registration = ReasonerRegistration(
            reasoner_id="other-reasoner",
            reasoner_name="Other Reasoner",
            allowed_gate_ids=frozenset({"policy"}),
            allowed_prompt_template_ids=frozenset({"stub-v1"}),
            allowed_jurisdictions=frozenset({"REGION_A"}),
        )
        self.router = APIRouter(prefix="/api/v1/other")
        self.prompts_dir = prompts_dir
        self.corpus_dir = prompts_dir
        self.raw_event_factory = dict

    def build_services(self, framework: FrameworkServices) -> Any:
        return MagicMock()


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every infrastructure constructor in ``app.bootstrap`` with a fake.

    Returns the fakes plus a shared ``calls`` journal recording the order in
    which the DDL was applied and pgvector adapters were registered.
    """
    calls: list[str] = []
    pg_conn = MagicMock(name="pg_conn")
    es = MagicMock(name="es")
    embed_model = MagicMock(name="embed_model")
    cross_encoder = MagicMock(name="cross_encoder")
    llm_client = MagicMock(name="llm_client", model="fake-llm")

    def _connect(dsn: str) -> MagicMock:
        calls.append(f"connect:{dsn}")
        return pg_conn

    def _apply_schema(conn: Any) -> None:
        assert conn is pg_conn
        calls.append("apply_canonical_schema")

    def _register_vector(conn: Any) -> None:
        assert conn is pg_conn
        calls.append("register_vector")

    monkeypatch.setattr(bootstrap.psycopg, "connect", _connect)
    monkeypatch.setattr(bootstrap, "apply_canonical_schema", _apply_schema)
    monkeypatch.setattr(bootstrap, "register_vector", _register_vector)
    monkeypatch.setattr(bootstrap, "Elasticsearch", lambda url: es)
    monkeypatch.setattr(bootstrap, "SentenceTransformer", lambda name: embed_model)
    monkeypatch.setattr(bootstrap, "CrossEncoder", lambda name: cross_encoder)
    monkeypatch.setattr(bootstrap, "OpenAILLMClient", lambda: llm_client)
    monkeypatch.setattr(bootstrap, "BundleStore", _RecordingStore)
    _RecordingStore.last_factories = None
    _RecordingStore.last_registry = None
    return {
        "calls": calls,
        "pg_conn": pg_conn,
        "es": es,
        "embed_model": embed_model,
        "cross_encoder": cross_encoder,
        "llm_client": llm_client,
    }


@pytest.fixture
def settings() -> FrameworkSettings:
    return FrameworkSettings(
        postgres_host="db.example",
        postgres_port=5433,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        elasticsearch_url="http://es.example:9200",
        openai_api_key="",
    )


def test_bootstrap_returns_framework_services_bound_to_the_settings(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [PLUGIN])

    assert isinstance(services, FrameworkServices)
    assert services.settings is settings
    assert services.pg_conn is fakes["pg_conn"]
    assert services.es is fakes["es"]
    assert isinstance(services.retriever, PolicyRetriever)
    assert isinstance(services.gate, PolicyGate)
    assert isinstance(services.store, BundleStore)
    assert fakes["calls"][0] == f"connect:{settings.postgres_dsn}"


def test_bootstrap_applies_canonical_schema_before_registering_pgvector(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    bootstrap.build_framework_services(settings, [PLUGIN])

    order = [c for c in fakes["calls"] if not c.startswith("connect:")]
    assert order == ["apply_canonical_schema", "register_vector"]


def test_bootstrap_loads_prompt_registry_from_each_plugin_prompts_dir(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [PLUGIN])

    assert services.prompt_registry.registered_ids() == ["stub-v1"]


def test_bootstrap_gives_the_store_one_raw_event_factory_per_reasoner(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    bootstrap.build_framework_services(settings, [PLUGIN])

    assert _RecordingStore.last_factories is not None
    assert set(_RecordingStore.last_factories) == {STUB_REGISTRATION.reasoner_id}
    assert (
        _RecordingStore.last_factories[STUB_REGISTRATION.reasoner_id]
        is PLUGIN.raw_event_factory
    )


def test_bootstrap_registry_holds_each_plugin_registration(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [PLUGIN])

    assert services.registry.get(STUB_REGISTRATION.reasoner_id) is STUB_REGISTRATION
    assert _RecordingStore.last_registry is services.registry


def test_bootstrap_with_no_plugins_builds_an_empty_registry_and_store(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    services = bootstrap.build_framework_services(settings, [])

    assert services.prompt_registry.registered_ids() == []
    assert not services.registry.is_registered(STUB_REGISTRATION.reasoner_id)
    assert _RecordingStore.last_factories == {}


def test_bootstrap_rejects_two_plugins_shipping_the_same_template_id(
    fakes: dict[str, Any], settings: FrameworkSettings
):
    colliding = _OtherPlugin(PLUGIN.prompts_dir)

    with pytest.raises(ValueError, match="stub-v1"):
        bootstrap.build_framework_services(settings, [PLUGIN, colliding])
