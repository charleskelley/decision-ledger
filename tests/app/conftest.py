"""Fixtures for the ledger API tests: the real app over in-memory services.

``ledger`` starts ``create_app`` with the stub reasoner and a fresh scripted
fake gate, and replaces only the bootstrap with ``memory_framework`` — the
routes, auth, problem handlers, operations, enforcement, and replay are the
production code. Requests authenticate with the keys in ``TEST_KEYRING``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from app.auth import KEY_HEADER
from app.main import create_app
from tests._fake_gate.plugin import FakeGatePlugin
from tests._memory_ledger import (
    OPERATOR_KEY,
    OTHER_KEY,
    STUB_KEY,
    TEST_KEYRING,
    MemoryBundleStore,
    MemoryJournal,
    memory_framework,
)
from tests._stub_reasoner.plugin import PLUGIN

if TYPE_CHECKING:
    from collections.abc import Iterator

    from app.plugin import FrameworkServices
    from core.submission import Submission


@dataclass
class Ledger:
    """A running ledger app over in-memory services."""

    client: TestClient
    framework: FrameworkServices
    gate_plugin: FakeGatePlugin

    @property
    def store(self) -> MemoryBundleStore:
        assert isinstance(self.framework.store, MemoryBundleStore)
        return self.framework.store

    @property
    def journal(self) -> MemoryJournal:
        assert isinstance(self.framework.journal, MemoryJournal)
        return self.framework.journal

    def post(
        self, path: str, body: str | bytes, *, key: str | None = STUB_KEY, **kw: Any
    ):
        headers = {KEY_HEADER: key} if key else {}
        return self.client.post(path, content=body, headers=headers, **kw)

    def get(self, path: str, *, key: str | None = STUB_KEY, **kw: Any):
        headers = {KEY_HEADER: key} if key else {}
        return self.client.get(path, headers=headers, **kw)

    def put(
        self, path: str, body: str | bytes, *, key: str | None = OPERATOR_KEY, **kw: Any
    ):
        headers = {KEY_HEADER: key} if key else {}
        return self.client.put(path, content=body, headers=headers, **kw)

    def submit(self, submission: Submission, *, key: str | None = STUB_KEY):
        return self.post("/api/v1/decisions", submission.model_dump_json(), key=key)


def start_ledger(
    monkeypatch: pytest.MonkeyPatch,
    framework: FrameworkServices,
    gate_plugin: FakeGatePlugin,
) -> Iterator[Ledger]:
    monkeypatch.setattr(
        "app.main.build_framework_services",
        lambda settings, plugins, gate_plugins: framework,
    )
    app = create_app(plugins=[PLUGIN], gate_plugins=[gate_plugin], keyring=TEST_KEYRING)
    with TestClient(app) as client:
        yield Ledger(client=client, framework=framework, gate_plugin=gate_plugin)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> Iterator[Ledger]:
    """The ledger app: stub reasoner, one fresh fake gate, in-memory services."""
    gate_plugin = FakeGatePlugin()
    framework = memory_framework([PLUGIN], [gate_plugin])
    yield from start_ledger(monkeypatch, framework, gate_plugin)


__all__ = ["OPERATOR_KEY", "OTHER_KEY", "STUB_KEY", "Ledger", "start_ledger"]
