"""Configuration of the conformance run — which instance, which keys.

Environment:

- ``DECISIONLEDGER_URL`` — the instance (required; every test skips without
  it).
- ``DECISIONLEDGER_KEY`` — the example reasoner's API key (required by every
  test that writes or reads a decision).
- ``DECISIONLEDGER_OTHER_KEY`` — another reasoner's key (403 and tenant
  isolation checks; skipped without it).
- ``DECISIONLEDGER_OPERATOR_KEY`` — the operator key (resolution and
  operator checks; skipped without it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from conformance._ledger import Ledger, env

if TYPE_CHECKING:
    from collections.abc import Iterator


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.conformance)


@pytest.fixture(scope="session")
def ledger() -> Iterator[Ledger]:
    url = env("DECISIONLEDGER_URL")
    if url is None:
        pytest.skip("set DECISIONLEDGER_URL to the instance under test")
    with httpx.Client(base_url=url, timeout=90.0) as http:
        yield Ledger(
            http=http,
            key=env("DECISIONLEDGER_KEY"),
            other_key=env("DECISIONLEDGER_OTHER_KEY"),
            operator_key=env("DECISIONLEDGER_OPERATOR_KEY"),
        )


@pytest.fixture
def key(ledger: Ledger) -> str:
    if ledger.key is None:
        pytest.skip("set DECISIONLEDGER_KEY (the example reasoner's API key)")
    return ledger.key


@pytest.fixture
def other_key(ledger: Ledger) -> str:
    if ledger.other_key is None:
        pytest.skip("set DECISIONLEDGER_OTHER_KEY (another reasoner's API key)")
    return ledger.other_key


@pytest.fixture
def operator_key(ledger: Ledger) -> str:
    if ledger.operator_key is None:
        pytest.skip("set DECISIONLEDGER_OPERATOR_KEY (the operator key)")
    return ledger.operator_key
