"""Fixtures for the stream mock's tests: no infrastructure.

``ledger`` is the framework composed in memory the way the bootstrap does
(``tests._memory_ledger.memory_framework``: the real registry, gate
admission, enforcement, store semantics, and replay over in-test storage),
with the mock's plugin and its scripted policy double (``policy_gate``), so
the mock's pipeline, service, and eval driver run through ``LocalClient`` —
the operation the ledger's HTTP route runs — without Postgres,
Elasticsearch, or a model. ``transfer`` builds one event;
``registration_file`` is the member's generated ``registration.yaml``.

Member tests have no ``__init__.py`` (pytest's importlib mode): shared
helpers are fixtures here, the member's ``testing.py``, or ``tests.*`` at
the repository root.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mock_stream_reasoner.events import TransferEvent
from mock_stream_reasoner.plugin import PLUGIN
from mock_stream_reasoner.testing import ScriptedPolicyGate, ScriptedPolicyGatePlugin
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.plugin import FrameworkServices

#: The member's directory (``reasoners/mock_stream``).
MEMBER_DIR = Path(__file__).resolve().parent.parent
#: The generated registration file the member ships.
REGISTRATION_FILE = MEMBER_DIR / "registration.yaml"
#: The event time the tests' streams start at.
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def policy_gate() -> ScriptedPolicyGate:
    """The scripted policy double (CHALLENGE at confidence 0.9)."""
    return ScriptedPolicyGate()


@pytest.fixture
def ledger(policy_gate: ScriptedPolicyGate) -> FrameworkServices:
    """The ledger in memory, serving the mock by its plugin, with the double."""
    return memory_framework([PLUGIN], [ScriptedPolicyGatePlugin(policy_gate)])


@pytest.fixture
def registration_file() -> Path:
    """The generated registration file the member ships (``registration.yaml``)."""
    return REGISTRATION_FILE


@pytest.fixture
def t0() -> datetime:
    """The event time the tests' streams start at."""
    return T0


@pytest.fixture
def transfer() -> Callable[..., TransferEvent]:
    """Build one transfer: ``transfer(n, account, amount, minutes=...)``."""

    def make(
        n: int, account: str, amount: float, *, minutes: float = 0.0
    ) -> TransferEvent:
        return TransferEvent(
            event_id=f"evt-test-{n:04d}",
            timestamp=T0 + timedelta(minutes=minutes),
            account_id=account,
            amount=amount,
        )

    return make
