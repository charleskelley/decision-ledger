"""Fixtures for the text mock's tests: no infrastructure.

``ledger`` is the framework composed in memory the way the bootstrap does
(``tests._memory_ledger.memory_framework``: the real registry, gate
admission, enforcement, store semantics, and replay over in-test storage),
with the mock's plugin and its scripted policy double (``policy_gate``, which
permits every message's proposal), so the mock's pipeline, CLI, and eval
driver run through ``LocalClient`` (``client``: the operation the ledger's
HTTP route runs) without Postgres, Elasticsearch, or a model.
``holding_ledger`` (``holding_client``) is the same deployment with a double
that answers a hardship message the way the RAG policy gate answers when its
model reports that the regulatory text and the practice notes disagree
(``holding_gate``: the evidence protocol's hold). ``message`` builds one
customer message; ``samples`` holds one representative message per label;
``member_dir`` and ``registration_file`` are the member's directory and its
generated ``registration.yaml``.

Member tests have no ``__init__.py`` (pytest's importlib mode): shared
helpers are fixtures here, the member's ``testing.py``, or ``tests.*`` at
the repository root.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.client import LocalClient
from mock_text_reasoner.messages import CustomerMessage
from mock_text_reasoner.plugin import PLUGIN
from mock_text_reasoner.testing import ScriptedPolicyGate, ScriptedPolicyGatePlugin
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.plugin import FrameworkServices

#: The member's directory (``reasoners/mock_text``).
MEMBER_DIR = Path(__file__).resolve().parent.parent
#: The generated registration file the member ships.
REGISTRATION_FILE = MEMBER_DIR / "registration.yaml"
#: When the tests' messages arrive.
RECEIVED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
#: Who the tests' messages are from, unless a test says otherwise.
CUSTOMER = "cust-test"
#: One representative text per label the classifier assigns, in ``LABELS``
#: order.
SAMPLE_TEXTS = {
    "credential_request": "I forgot my PIN, can you remind me what it is?",
    "fraud_report": "Please report fraud: there is a payment I did not make.",
    "account_change": "I moved house last week, please change my address.",
    "hardship": "I lost my job and can't pay this month's installment.",
    "general_inquiry": "What are your opening hours on Saturday?",
    "unclassified": "Do you sponsor the local football team this season?",
}


@pytest.fixture
def policy_gate() -> ScriptedPolicyGate:
    """The scripted policy double: it permits each message's proposal (0.9)."""
    return ScriptedPolicyGate()


@pytest.fixture
def ledger(policy_gate: ScriptedPolicyGate) -> FrameworkServices:
    """The ledger in memory, serving the mock by its plugin, with the double."""
    return memory_framework([PLUGIN], [ScriptedPolicyGatePlugin(policy_gate)])


@pytest.fixture
def client(ledger: FrameworkServices) -> LocalClient:
    """``ledger`` as the trusted principal, through its operations."""
    return LocalClient(ledger)


@pytest.fixture
def holding_gate() -> ScriptedPolicyGate:
    """The double, holding a hardship message as the gate's evidence protocol does."""
    return ScriptedPolicyGate(protocol_hold_labels={"hardship"})


@pytest.fixture
def holding_ledger(holding_gate: ScriptedPolicyGate) -> FrameworkServices:
    """The ledger in memory, serving the mock by its plugin, with the holding double."""
    return memory_framework([PLUGIN], [ScriptedPolicyGatePlugin(holding_gate)])


@pytest.fixture
def holding_client(holding_ledger: FrameworkServices) -> LocalClient:
    """``holding_ledger`` as the trusted principal, through its operations."""
    return LocalClient(holding_ledger)


@pytest.fixture
def message() -> Callable[..., CustomerMessage]:
    """Build one message: ``message(n, text, customer=..., channel=...)``.

    Event id ``msg-test-<n>``, received at ``RECEIVED_AT``, from ``CUSTOMER``
    on chat unless the call says otherwise.
    """

    def make(
        n: int, text: str, *, customer: str = CUSTOMER, channel: str = "chat"
    ) -> CustomerMessage:
        return CustomerMessage(
            event_id=f"msg-test-{n:04d}",
            received_at=RECEIVED_AT,
            customer_id=customer,
            channel=channel,
            text=text,
        )

    return make


@pytest.fixture
def samples(message: Callable[..., CustomerMessage]) -> dict[str, CustomerMessage]:
    """One representative message per label: ``{label: message}``."""
    return {
        label: message(n, text)
        for n, (label, text) in enumerate(SAMPLE_TEXTS.items(), 1)
    }


@pytest.fixture
def member_dir() -> Path:
    """The member's directory (``reasoners/mock_text``)."""
    return MEMBER_DIR


@pytest.fixture
def registration_file() -> Path:
    """The generated registration file the member ships (``registration.yaml``)."""
    return REGISTRATION_FILE
