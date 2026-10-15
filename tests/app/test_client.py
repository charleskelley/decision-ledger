"""Behavioral tests for ``app.client`` — ``LocalClient`` and ``HttpClient``.

``LocalClient`` serializes the same ``Submission`` and calls the same
operation the HTTP route calls (DR-31); ``HttpClient`` speaks to the route.
Both run here against one ledger app (``HttpClient`` through
``httpx.ASGITransport``, no network), and must agree:

- the same receipt for the same submission, and the same ``LedgerError``
  (status and code) for the same refusal;
- the same lookup, typed bundle, replay, batch, timeline, resolution, and
  resources;
- ``get_bundle`` refuses a bundle older than schema version 2;
- ``LocalClient`` acts as the trusted principal unless given another, and
  ``from_settings`` composes the deployment like ``create_app`` and owns it;
- a non-problem error response still raises ``LedgerError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.api.models import ResolutionRequest
from app.auth import Principal
from app.client import HttpClient, LedgerClient, LocalClient, raise_for_problem
from core.actions import DecisionAction
from core.bundle import DecisionBundle
from core.exceptions import LedgerError
from core.submission import BypassCitation
from tests._memory_ledger import OPERATOR_KEY, STUB_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from core.submission import Submission
    from tests.app.conftest import Ledger


@pytest.fixture
async def clients(ledger: Ledger) -> AsyncIterator[dict[str, LedgerClient]]:
    http = HttpClient(
        "http://ledger.test",
        api_key=STUB_KEY,
        operator_key=OPERATOR_KEY,
        transport=httpx.ASGITransport(app=ledger.client.app),
    )
    local = LocalClient(ledger.framework)
    yield {"local": local, "http": http}
    await http.aclose()
    await local.aclose()


def _event(submission: Submission, name: str) -> Submission:
    return submission.model_copy(update={"event_id": f"evt-{name}"})


def _comparable(receipt: Any) -> dict[str, Any]:
    data = receipt.model_dump(mode="json")
    for varying in ("decision_id", "event_id", "created_at", "latency_breakdown"):
        data.pop(varying)
    return data


async def test_both_clients_record_and_gate_alike(
    clients: dict[str, LedgerClient], submission, gated_submission
):
    receipts = {
        name: (
            await client.submit(_event(submission, name)),
            await client.submit(_event(gated_submission, f"{name}-gated")),
        )
        for name, client in clients.items()
    }

    local_record, local_gated = receipts["local"]
    http_record, http_gated = receipts["http"]
    assert _comparable(local_record) == _comparable(http_record)
    assert _comparable(local_gated) == _comparable(http_gated)
    assert local_record.decision_action == DecisionAction.ALLOW
    assert local_gated.mode == "gate"


async def test_both_clients_raise_the_same_problem(
    clients: dict[str, LedgerClient], submission
):
    bad = submission.model_copy(update={"bypass": BypassCitation(rule_id="nope")})
    errors = {}
    for name, client in clients.items():
        with pytest.raises(LedgerError) as err:
            await client.submit(bad)
        errors[name] = (err.value.status, err.value.code, err.value.extra)
    assert errors["local"] == errors["http"]
    assert errors["local"][:2] == (422, "BYPASS_RULE_UNREGISTERED")


async def test_both_clients_read_the_same_records(
    clients: dict[str, LedgerClient], submission
):
    receipt = await clients["local"].submit(submission)

    for client in clients.values():
        document = await client.get_decision(receipt.decision_id)
        assert document["decision_id"] == receipt.decision_id
        bundle = await client.get_bundle(receipt.decision_id)
        assert isinstance(bundle, DecisionBundle)
        assert bundle.submission == submission
        replay = await client.replay(receipt.decision_id)
        assert replay.matched
        timeline = await client.entity_decisions(submission.entity_id)
        assert [d.decision_id for d in timeline.decisions] == [receipt.decision_id]
        reasoners = await client.reasoners()
        assert [r.reasoner_id for r in reasoners.reasoners] == ["stub-reasoner"]
        one = await client.reasoner("stub-reasoner")
        assert one.registration_version == receipt.registration_version
        gates = await client.gates()
        assert [g.gate_id for g in gates.gates] == ["fake"]


async def test_get_bundle_refuses_an_older_bundle(
    ledger: Ledger, clients: dict[str, LedgerClient]
):
    decision_id = ledger.store.insert_raw(
        {"decision_action": "HOLD"}, reasoner_id="stub-reasoner", schema_version=1
    )
    for client in clients.values():
        assert (await client.get_decision(decision_id))["schema_version"] == 1
        with pytest.raises(LedgerError) as err:
            await client.get_bundle(decision_id)
        assert (err.value.status, err.value.code) == (409, "BUNDLE_SCHEMA_UNSUPPORTED")


async def test_both_clients_batch_alike(clients: dict[str, LedgerClient], submission):
    results = {}
    for name, client in clients.items():
        batch = [_event(submission, f"{name}-{n}") for n in range(3)]
        results[name] = await client.submit_batch(batch)
    for result in results.values():
        assert (result.recorded, result.rejected) == (3, 0)
        assert [r.line for r in result.results] == [1, 2, 3]


async def test_both_clients_resolve_a_held_decision(
    clients: dict[str, LedgerClient], submission
):
    context = submission.reasoner_context.model_copy(update={"label_value": 0.5})
    for name, client in clients.items():
        held = _event(submission, name).model_copy(update={"reasoner_context": context})
        receipt = await client.submit(held)
        assert receipt.decision_action == DecisionAction.HOLD
        resolved = await client.resolve(
            receipt.decision_id,
            ResolutionRequest(
                resolver_id="analyst", resolution_action=DecisionAction.BLOCK, note="no"
            ),
        )
        assert resolved.realized_action == DecisionAction.BLOCK


async def test_a_local_client_acts_as_the_principal_it_is_given(
    ledger: Ledger, submission
):
    other = LocalClient(
        ledger.framework, principal=Principal.for_reasoner("other-reasoner")
    )
    with pytest.raises(LedgerError) as err:
        await other.submit(submission)
    assert (err.value.status, err.value.code) == (403, "REASONER_MISMATCH")

    trusted = LocalClient(ledger.framework).with_framework(ledger.framework)
    assert (await trusted.submit(submission)).decision_action == DecisionAction.ALLOW


async def test_from_settings_composes_the_deployment_and_owns_it(
    monkeypatch: pytest.MonkeyPatch,
):
    framework = MagicMock(name="framework")
    seen: dict[str, Any] = {}

    def _build(settings: Any, plugins: Any, gate_plugins: Any) -> MagicMock:
        seen.update(plugins=plugins, gate_plugins=gate_plugins)
        return framework

    monkeypatch.setattr("app.bootstrap.build_framework_services", _build)
    monkeypatch.setattr("app.plugins.discover_plugins", lambda *, allow: ["p"])
    monkeypatch.setattr("app.gates.discover_gates", lambda: ["g"])

    client = LocalClient.from_settings()
    assert client.framework is framework
    assert seen == {"plugins": ["p"], "gate_plugins": ["g"]}
    await client.aclose()
    framework.close.assert_called_once()


async def test_a_client_it_does_not_own_leaves_the_framework_open(ledger: Ledger):
    async with LocalClient(ledger.framework):
        pass
    assert not ledger.framework.pg_conn.closed  # type: ignore[attr-defined]


async def test_the_http_client_sends_no_key_it_does_not_have(
    ledger: Ledger, submission
):
    async with HttpClient(
        "http://ledger.test", transport=httpx.ASGITransport(app=ledger.client.app)
    ) as anonymous:
        with pytest.raises(LedgerError) as err:
            await anonymous.submit(submission)
    assert (err.value.status, err.value.code) == (401, "UNAUTHENTICATED")


def test_a_non_problem_error_response_still_raises():
    response = httpx.Response(
        502, text="bad gateway", request=httpx.Request("GET", "/")
    )
    with pytest.raises(LedgerError) as err:
        raise_for_problem(response)
    assert (err.value.status, err.value.code) == (502, "HTTP_502")
    raise_for_problem(httpx.Response(200, request=httpx.Request("GET", "/")))


def test_both_clients_satisfy_the_protocol(ledger: Ledger):
    assert isinstance(LocalClient(ledger.framework), LedgerClient)
    assert isinstance(HttpClient("http://x"), LedgerClient)


@pytest.mark.parametrize("limit", [0, 501])
async def test_both_clients_refuse_an_out_of_range_limit_alike(
    clients: dict[str, LedgerClient], submission, limit: int
):
    for client in clients.values():
        with pytest.raises(LedgerError) as err:
            await client.entity_decisions(submission.entity_id, limit=limit)
        assert (err.value.status, err.value.code) == (422, "REQUEST_INVALID")


def test_the_http_client_strips_keys_and_refuses_unsendable_ones():
    client = HttpClient("http://x", api_key="  k-1 ", operator_key=" ")
    assert client._api_key == "k-1"
    assert client._operator_key is None
    for bad in ("k 2", "k\x00", "ké"):
        with pytest.raises(ValueError, match="api_key must be visible ASCII") as err:
            HttpClient("http://x", api_key=bad)
        assert bad not in str(err.value)


def test_a_batch_waits_for_the_ledger_and_a_decision_outlasts_two_gate_phases():
    client = HttpClient("http://x")
    assert client._batch_timeout.read is None
    assert client._timeout > 2 * 45
