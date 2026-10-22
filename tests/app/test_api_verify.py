"""Behavioral tests for verify (DR-34): ``POST /api/v1/decisions/{id}/verify``.

The ledger app runs over in-memory services (``tests/app/conftest``) with the
stub reasoner and the scripted fake gates, whose ``gate_config`` scripts the
evidence a gate cites, whether it escrows it, and what its ``verify``
answers (``tests/_fake_gate``), so every status is reachable without Docker.
What is pinned:

- Each evidence reference of each gate that ran gets two independent
  answers: ``escrow`` (the framework's: does the evidence escrow hold the
  artifact it names? ``present``, ``missing``, or ``none``) and ``status``
  (its gate's, stored or live), counted in the summary.
- A gate that is not composed, does not verify, fails, answers late, or
  answers out of contract leaves its references ``not_verifiable``; a
  failed gate and a record no gate ran on contribute no references; a
  mandatory gate's references on a record are verified.
- Escrow off: gates name no artifact (``escrow: none``).
- Verify decides nothing again: the bundle is unchanged, no gate is
  evaluated, and replay still matches.
- Access is the lookup's: another tenant's decision is not found, the
  operator verifies any, an older bundle is 409. ``LocalClient.verify``
  answers as the route does.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest

from app.client import LocalClient
from app.gates import ComposedGate, verify_evidence
from core.actions import DecisionAction
from core.gate import EvidenceRef, GateOutput, GateVerdict
from core.observation import GateContext
from tests._fake_gate.plugin import (
    FakeGatePlugin,
    FakeLookupGatePlugin,
    fake_evidence,
)
from tests._memory_ledger import (
    OPERATOR_KEY,
    OTHER_KEY,
    STUB_KEY,
    MemoryArtifacts,
    memory_framework,
)
from tests._stub_reasoner.plugin import StubPlugin
from tests._stub_reasoner.registry import STUB_REGISTRATION_WITH_LOOKUP
from tests.app.conftest import start_ledger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.submission import Submission
    from tests.app.conftest import Ledger

_STATUSES = ("intact", "source_changed", "source_unavailable", "not_verifiable")


def _scripted(submission: Submission, **config: Any) -> Submission:
    return submission.model_copy(
        update={"gate_contexts": (GateContext(gate_id="fake", gate_config=config),)}
    )


def _addressing_the_lookup(submission: Submission) -> Submission:
    """The submission, also addressing the lookup gate its variant binds."""
    lookup = GateContext(gate_id="fake-lookup", gate_config={})
    return submission.model_copy(
        update={"gate_contexts": (*submission.gate_contexts, lookup)}
    )


def _decide(ledger: Ledger, submission: Submission) -> str:
    response = ledger.submit(submission)
    assert response.status_code == 201, response.text
    return response.json()["decision_id"]


def _verify(
    ledger: Ledger, decision_id: str, *, live: bool | None = None, key: str = STUB_KEY
):
    query = "" if live is None else f"?live={'true' if live else 'false'}"
    return ledger.post(f"/api/v1/decisions/{decision_id}/verify{query}", b"", key=key)


def _verified(ledger: Ledger, decision_id: str, **kw: Any) -> dict[str, Any]:
    response = _verify(ledger, decision_id, **kw)
    assert response.status_code == 200, response.text
    return response.json()


def _digest(source: str) -> str:
    return hashlib.sha256(fake_evidence(source)).hexdigest()


# ---------------------------------------------------------------------------
# What a reference reports
# ---------------------------------------------------------------------------


def test_escrowed_evidence_verifies_intact_with_its_escrow_present(
    ledger: Ledger, gated_submission
):
    decision_id = _decide(
        ledger,
        _scripted(
            gated_submission,
            permitted_actions=["CHALLENGE"],
            evidence_sources=["doc-a", "doc-b"],
            escrow_evidence=True,
        ),
    )

    result = _verified(ledger, decision_id)

    assert result["decision_id"] == decision_id
    assert result["live"] is False
    assert result["checked_at"]
    assert result["refs"] == [
        {
            "gate_id": "fake",
            "source_id": source,
            "locator": None,
            "content_hash": _digest(source),
            "artifact_hash": _digest(source),
            "escrow": "present",
            "status": "intact",
        }
        for source in ("doc-a", "doc-b")
    ]
    assert result["summary"] == {
        "intact": 2,
        "changed": 0,
        "unavailable": 0,
        "not_verifiable": 0,
        "escrow_missing": 0,
    }
    # What the escrow holds is what the gate relied on.
    assert ledger.store.artifacts.get(_digest("doc-a")) == fake_evidence("doc-a")


def test_live_verify_asks_the_gate_about_its_sources_as_they_are_now(
    ledger: Ledger, gated_submission
):
    decision_id = _decide(
        ledger,
        _scripted(
            gated_submission,
            evidence_sources=["doc-a"],
            escrow_evidence=True,
            verify_live=["source_changed"],
        ),
    )

    stored = _verified(ledger, decision_id, live=False)
    live = _verified(ledger, decision_id, live=True)

    assert [r["status"] for r in stored["refs"]] == ["intact"]
    assert [r["status"] for r in live["refs"]] == ["source_changed"]
    assert live["live"] is True
    assert live["refs"][0]["escrow"] == "present"  # independent of the status
    [gate] = ledger.gate_plugin.built
    assert [live for _, live in gate.verifications] == [False, True]


def test_every_status_a_gate_answers_is_reported_and_counted(
    ledger: Ledger, gated_submission
):
    sources = [f"doc-{n}" for n in range(4)]
    decision_id = _decide(
        ledger,
        _scripted(
            gated_submission,
            evidence_sources=sources,
            escrow_evidence=True,
            verify_stored=list(_STATUSES),
        ),
    )

    result = _verified(ledger, decision_id)

    assert [r["status"] for r in result["refs"]] == list(_STATUSES)
    assert result["summary"] == {
        "intact": 1,
        "changed": 1,
        "unavailable": 1,
        "not_verifiable": 1,
        "escrow_missing": 0,
    }


def test_escrow_is_none_when_the_gate_escrowed_nothing(
    ledger: Ledger, gated_submission
):
    decision_id = _decide(
        ledger, _scripted(gated_submission, evidence_sources=["doc-a"])
    )

    [ref] = _verified(ledger, decision_id)["refs"]

    assert (ref["artifact_hash"], ref["escrow"]) == (None, "none")
    assert ref["content_hash"] == _digest("doc-a")


def test_escrow_is_missing_when_the_escrow_no_longer_holds_the_artifact(
    ledger: Ledger, gated_submission
):
    decision_id = _decide(
        ledger,
        _scripted(
            gated_submission,
            evidence_sources=["doc-a", "doc-b"],
            escrow_evidence=True,
        ),
    )
    artifacts = ledger.store.artifacts
    assert isinstance(artifacts, MemoryArtifacts)
    del artifacts.rows[_digest("doc-b")]

    result = _verified(ledger, decision_id)

    assert [r["escrow"] for r in result["refs"]] == ["present", "missing"]
    assert result["summary"]["escrow_missing"] == 1


@pytest.fixture
def escrow_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[Ledger]:
    """The ledger with escrow off (``DECISIONLEDGER_EVIDENCE_ESCROW=off``)."""
    gate_plugin = FakeGatePlugin()
    framework = memory_framework(
        [StubPlugin()], [gate_plugin], artifacts=MemoryArtifacts(enabled=False)
    )
    yield from start_ledger(monkeypatch, framework, gate_plugin)


def test_with_escrow_off_references_name_no_artifact(
    escrow_off: Ledger, gated_submission
):
    decision_id = _decide(
        escrow_off,
        _scripted(gated_submission, evidence_sources=["doc-a"], escrow_evidence=True),
    )

    [ref] = _verified(escrow_off, decision_id)["refs"]

    assert (ref["artifact_hash"], ref["escrow"]) == (None, "none")
    artifacts = escrow_off.store.artifacts
    assert isinstance(artifacts, MemoryArtifacts)
    assert artifacts.rows == {}


# ---------------------------------------------------------------------------
# Which gates are asked
# ---------------------------------------------------------------------------


@pytest.fixture
def with_lookup(monkeypatch: pytest.MonkeyPatch) -> Iterator[Ledger]:
    """The stub bound to a mandatory lookup gate (which does not verify)."""
    plugin = StubPlugin()
    plugin.registration = STUB_REGISTRATION_WITH_LOOKUP
    lookup = FakeLookupGatePlugin()
    gate_plugin = FakeGatePlugin()
    framework = memory_framework([plugin], [lookup, gate_plugin])
    for ledger in start_ledger(monkeypatch, framework, gate_plugin):
        ledger.lookup = lookup  # type: ignore[attr-defined]
        yield ledger


def test_a_gate_that_does_not_verify_leaves_its_references_not_verifiable(
    with_lookup: Ledger, gated_submission
):
    with_lookup.lookup.listed[gated_submission.entity_id] = DecisionAction.HOLD  # type: ignore[attr-defined]
    decision_id = _decide(
        with_lookup,
        _addressing_the_lookup(
            _scripted(
                gated_submission, evidence_sources=["doc-a"], escrow_evidence=True
            )
        ),
    )

    lookup, fake = _verified(with_lookup, decision_id)["refs"]

    # The lookup escrowed the row it answered from; it does not verify.
    assert (lookup["gate_id"], lookup["escrow"], lookup["status"]) == (
        "fake-lookup",
        "present",
        "not_verifiable",
    )
    assert (fake["gate_id"], fake["escrow"], fake["status"]) == (
        "fake",
        "present",
        "intact",
    )


def test_a_mandatory_gates_references_on_a_record_are_verified(
    with_lookup: Ledger, submission
):
    with_lookup.lookup.listed[submission.entity_id] = DecisionAction.ALLOW  # type: ignore[attr-defined]
    decision_id = _decide(with_lookup, _addressing_the_lookup(submission))

    [ref] = _verified(with_lookup, decision_id)["refs"]

    assert (ref["gate_id"], ref["escrow"]) == ("fake-lookup", "present")


def test_a_record_no_gate_ran_on_has_no_references(ledger: Ledger, submission):
    decision_id = _decide(ledger, submission)

    result = _verified(ledger, decision_id)

    assert result["refs"] == []
    assert result["summary"] == {
        "intact": 0,
        "changed": 0,
        "unavailable": 0,
        "not_verifiable": 0,
        "escrow_missing": 0,
    }


def test_a_failed_gate_contributes_no_references(ledger: Ledger, gated_submission):
    decision_id = _decide(
        ledger,
        _scripted(gated_submission, evidence_sources=["doc-a"], raise_error=True),
    )

    assert _verified(ledger, decision_id)["refs"] == []


def test_a_gate_no_longer_composed_leaves_its_references_not_verifiable(
    ledger: Ledger, gated_submission
):
    decision_id = _decide(
        ledger,
        _scripted(gated_submission, evidence_sources=["doc-a"], escrow_evidence=True),
    )
    without_gates = dataclasses.replace(ledger.framework, gates=MappingProxyType({}))

    result = asyncio.run(LocalClient(without_gates).verify(decision_id))

    [ref] = result.refs
    assert (ref.escrow, ref.status) == ("present", "not_verifiable")


@pytest.mark.parametrize(
    "script",
    [
        {"verify_raises": True},
        {"verify_stored": ["intact"]},  # one answer for two references
        {"verify_stored": ["intact", "fine"]},  # not a status
    ],
    ids=["raises", "too-few", "unknown-status"],
)
def test_a_gate_that_fails_or_answers_out_of_contract_is_not_verifiable(
    ledger: Ledger, gated_submission, script
):
    decision_id = _decide(
        ledger,
        _scripted(
            gated_submission,
            evidence_sources=["doc-a", "doc-b"],
            escrow_evidence=True,
            **script,
        ),
    )

    result = _verified(ledger, decision_id)

    assert [r["status"] for r in result["refs"]] == ["not_verifiable"] * 2
    assert [r["escrow"] for r in result["refs"]] == ["present"] * 2


# ---------------------------------------------------------------------------
# Verify is not replay
# ---------------------------------------------------------------------------


def test_verify_decides_nothing_again_and_replay_still_matches(
    ledger: Ledger, gated_submission
):
    decision_id = _decide(
        ledger,
        _scripted(
            gated_submission,
            permitted_actions=["HOLD"],
            evidence_sources=["doc-a"],
            escrow_evidence=True,
            verify_stored=["source_changed"],
        ),
    )
    before = ledger.get(f"/api/v1/decisions/{decision_id}").json()
    [gate] = ledger.gate_plugin.built
    evaluated = len(gate.requests)

    _verified(ledger, decision_id)
    _verified(ledger, decision_id, live=True)

    assert ledger.get(f"/api/v1/decisions/{decision_id}").json() == before
    assert len(gate.requests) == evaluated
    replay = ledger.post(f"/api/v1/decisions/{decision_id}/replay", b"")
    assert replay.json()["matched"] is True


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_another_tenants_decision_is_not_found_and_the_operator_verifies_any(
    ledger: Ledger, gated_submission
):
    decision_id = _decide(ledger, _scripted(gated_submission))

    other = _verify(ledger, decision_id, key=OTHER_KEY)
    operator = _verify(ledger, decision_id, key=OPERATOR_KEY)

    assert other.status_code == 404
    assert other.json()["code"] == "DECISION_NOT_FOUND"
    assert operator.status_code == 200


def test_an_unknown_decision_is_not_found_and_no_key_is_401(ledger: Ledger):
    unknown = _verify(ledger, "00000000-0000-0000-0000-000000000000")
    no_key = ledger.post("/api/v1/decisions/x/verify", b"", key=None)

    assert (unknown.status_code, unknown.json()["code"]) == (404, "DECISION_NOT_FOUND")
    assert no_key.status_code == 401


def test_an_older_bundle_is_409_as_replay_answers(ledger: Ledger):
    decision_id = ledger.store.insert_raw(
        {"decision_action": "HOLD"}, reasoner_id="stub-reasoner", schema_version=1
    )

    response = _verify(ledger, decision_id)

    assert response.status_code == 409
    assert response.json()["code"] == "BUNDLE_SCHEMA_UNSUPPORTED"


def test_the_local_client_verifies_as_the_route_does(ledger: Ledger, gated_submission):
    decision_id = _decide(
        ledger,
        _scripted(gated_submission, evidence_sources=["doc-a"], escrow_evidence=True),
    )

    local = asyncio.run(LocalClient(ledger.framework).verify(decision_id, live=True))
    route = _verified(ledger, decision_id, live=True)

    assert local.model_dump(mode="json", exclude={"checked_at"}) == {
        k: v for k, v in route.items() if k != "checked_at"
    }


# ---------------------------------------------------------------------------
# verify_evidence — the framework's half of asking a gate
# ---------------------------------------------------------------------------


def _output(*sources: str) -> GateOutput:
    return GateOutput(
        gate_id="slow",
        deterministic=False,
        verdict=GateVerdict(
            gate_id="slow",
            permitted_actions=[DecisionAction.ALLOW],
            required_controls=[],
            confidence=0.9,
            escalate_to_human=False,
            escalation_reason=None,
        ),
        evidence=tuple(EvidenceRef(source_id=s) for s in sources),
    )


class _Answering:
    """A gate that verifies after ``delay`` seconds, answering ``answer``."""

    def __init__(self, answer: Any, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.asked = 0

    async def evaluate(self, request: Any) -> GateOutput:
        raise AssertionError("verify never evaluates")

    async def verify(self, output: GateOutput, *, live: bool) -> Any:
        self.asked += 1
        await asyncio.sleep(self.delay)
        return self.answer

    def close(self) -> None:
        return None


def _composed(gate: Any) -> ComposedGate:
    return ComposedGate(plugin=FakeGatePlugin("slow"), gate=gate)


def test_a_gate_answering_after_its_budget_is_not_verifiable():
    gate = _Answering(("intact",), delay=0.2)

    statuses = asyncio.run(
        verify_evidence(_composed(gate), _output("a"), live=False, budget_ms=20)
    )

    assert statuses == ("not_verifiable",)


def test_an_output_that_references_nothing_asks_no_gate():
    gate = _Answering(())

    assert (
        asyncio.run(
            verify_evidence(_composed(gate), _output(), live=True, budget_ms=1_000)
        )
        == ()
    )
    assert gate.asked == 0


def test_a_list_of_known_statuses_is_accepted_as_the_answer():
    gate = _Answering(["source_unavailable", "intact"])

    statuses = asyncio.run(
        verify_evidence(_composed(gate), _output("a", "b"), live=True, budget_ms=1_000)
    )

    assert statuses == ("source_unavailable", "intact")


def test_no_composed_gate_is_not_verifiable():
    statuses = asyncio.run(
        verify_evidence(None, _output("a", "b"), live=False, budget_ms=1_000)
    )

    assert statuses == ("not_verifiable", "not_verifiable")


@pytest.mark.parametrize(
    "answer",
    [[{"status": "intact"}], [["intact"]], [None], "intact", None, 1],
    ids=["dict", "list", "none-item", "string", "none", "int"],
)
def test_an_answer_that_is_not_one_known_status_per_reference_is_not_verifiable(
    answer,
):
    gate = _Answering(answer)

    statuses = asyncio.run(
        verify_evidence(_composed(gate), _output("a"), live=False, budget_ms=1_000)
    )

    assert statuses == ("not_verifiable",)


class _CancelledInside(_Answering):
    """A gate whose own helper task is cancelled (not its caller)."""

    async def verify(self, output: GateOutput, *, live: bool) -> Any:
        helper = asyncio.ensure_future(asyncio.sleep(10))
        helper.cancel()
        return await helper


class _Blocking(_Answering):
    """A gate that blocks the event loop past its budget, then answers."""

    async def verify(self, output: GateOutput, *, live: bool) -> Any:
        time.sleep(0.05)
        return ("intact",) * len(output.evidence)


class _Stubborn(_Answering):
    """A gate that swallows its cancellation and answers late."""

    async def verify(self, output: GateOutput, *, live: bool) -> Any:
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
        return ("intact",) * len(output.evidence)


@pytest.mark.parametrize(
    "gate",
    [_CancelledInside(()), _Blocking(()), _Stubborn(())],
    ids=lambda g: type(g).__name__,
)
def test_a_gate_cancelled_on_its_own_or_answering_late_is_not_verifiable(gate):
    statuses = asyncio.run(
        verify_evidence(_composed(gate), _output("a"), live=True, budget_ms=20)
    )

    assert statuses == ("not_verifiable",)


def test_the_callers_own_cancellation_propagates():
    gate = _Answering(("intact",), delay=10)

    async def _cancelled_caller() -> None:
        task = asyncio.ensure_future(
            verify_evidence(_composed(gate), _output("a"), live=True, budget_ms=60_000)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_cancelled_caller())
