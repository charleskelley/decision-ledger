"""The six tools are thin, faithful calls on a ``LedgerClient``.

A recording client stands in for the ledger: each tool must call the right
client method with the right arguments and return its result as JSON; a
ledger refusal must come back as the problem document in an error result;
invalid arguments, the wrong tool for a submission's mode, and an
unreachable ledger are error results too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.api.models import (
    DecisionReceipt,
    EntityDecisions,
    ReplayResponse,
    ResolutionReceipt,
)
from core.actions import DecisionAction
from core.exceptions import LedgerError
from decisionledger_mcp.server import McpServer, ToolError
from decisionledger_mcp.tools import ledger_tools

_DECISION_ID = str(uuid.uuid4())
_ENTITY_ID = str(uuid.uuid4())


def _submission(mode: str | None = "record") -> dict[str, Any]:
    submission: dict[str, Any] = {
        "reasoner_id": "agent-reasoner",
        "event_id": "evt-agent-1",
        "timestamp": "2026-10-14T12:00:00Z",
        "entity_id": _ENTITY_ID,
        "entity_type": "customer",
        "proposed_action": "ALLOW",
        "bypass": {"rule_id": "confident-allow"},
        "reasoner_context": {
            "reasoner_id": "agent-reasoner",
            "reasoner_name": "Agent Reasoner",
            "model_version": "claude-opus-5-5",
            "label_type": "NUMERICAL",
            "label_name": "risk_score",
            "label_value": 0.05,
            "feature_set": {"tool_trace_sha256": "ab" * 32},
        },
        "gate_contexts": [],
        "raw_event": {"ticket": 42},
    }
    if mode is None:
        return submission
    if mode == "gate":
        submission.pop("bypass")
        submission.pop("proposed_action")
    return {**submission, "mode": mode}


def _receipt(mode: str) -> DecisionReceipt:
    return DecisionReceipt(
        decision_id=_DECISION_ID,
        reasoner_id="agent-reasoner",
        event_id="evt-agent-1",
        entity_id=_ENTITY_ID,
        mode=mode,  # type: ignore[arg-type]
        decision_action=DecisionAction.ALLOW,
        decision_level_rule=None,
        enforcement_rule_applied=None,
        bypass_rule_id=None,
        bypass_rule_holds=None,
        registration_version="v",
        gates=[],
        latency_breakdown={},
        created_at=datetime(2026, 10, 14, tzinfo=UTC),
    )


class _Recording:
    """A ledger client that records calls; ``fail`` makes every call raise it."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail = fail

    def _record(self, name: str, value: Any) -> None:
        self.calls.append((name, value))
        if self.fail is not None:
            raise self.fail

    async def submit(self, submission):
        self._record("submit", submission)
        return _receipt(submission.mode)

    async def get_decision(self, decision_id):
        self._record("get_decision", decision_id)
        return {"decision_id": decision_id, "schema_version": 2}

    async def replay(self, decision_id):
        self._record("replay", decision_id)
        return ReplayResponse(
            decision_id=decision_id,
            original_action="ALLOW",
            replayed_action="ALLOW",
            actions_match=True,
            original_bypass_rule_holds=True,
            bypass_rule_holds=True,
            gates=[],
            matched=True,
        )

    async def entity_decisions(
        self, entity_id, *, reasoner_id, limit, before, before_id=None
    ):
        self._record(
            "entity_decisions", (entity_id, reasoner_id, limit, before, before_id)
        )
        return EntityDecisions(entity_id=entity_id, decisions=[])

    async def resolve(self, decision_id, resolution):
        self._record("resolve", (decision_id, resolution))
        return ResolutionReceipt(
            decision_id=decision_id,
            attempt_id="a-1",
            sequence=0,
            resolver_kind="HUMAN",
            resolver_id=resolution.resolver_id,
            status="COMPLETED",
            resolution_action=resolution.resolution_action,
            realized_action=resolution.resolution_action,
            completed_at=datetime(2026, 10, 14, tzinfo=UTC),
        )


def _tools(client: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in ledger_tools(client)}


def test_the_six_tools_and_their_schemas():
    tools = _tools(_Recording())
    assert list(tools) == [
        "record_decision",
        "gate_decision",
        "lookup_decision",
        "replay_decision",
        "list_decisions_for_entity",
        "resolve_hold",
    ]
    record = tools["record_decision"].definition()
    schema = record["inputSchema"]
    assert schema["required"] == ["submission"]
    assert "ReasonerContext" in schema["$defs"]
    assert schema["properties"]["submission"]["properties"]["mode"]
    assert "tool_trace_sha256" in record["description"]
    for tool in tools.values():
        assert tool.description
        assert tool.input_schema["type"] == "object"
    assert tools["lookup_decision"].annotations["readOnlyHint"] is True
    assert tools["resolve_hold"].input_schema["properties"]["resolution_action"][
        "enum"
    ] == ["ALLOW", "BLOCK"]


@pytest.mark.parametrize(
    ("tool", "mode"), [("record_decision", "record"), ("gate_decision", "gate")]
)
async def test_a_submitting_tool_sets_its_mode_and_returns_the_receipt(tool, mode):
    client = _Recording()
    submission = _submission(None if mode == "record" else "gate")
    result = await _tools(client)[tool].handler({"submission": submission})

    [(name, submitted)] = client.calls
    assert name == "submit"
    assert submitted.mode == mode
    assert submitted.reasoner_context.model_version == "claude-opus-5-5"
    assert result["decision_id"] == _DECISION_ID
    assert result["mode"] == mode


async def test_the_wrong_tool_for_a_mode_is_refused():
    with pytest.raises(ToolError) as err:
        await _tools(_Recording())["gate_decision"].handler(
            {"submission": _submission("record")}
        )
    assert err.value.payload["code"] == "WRONG_TOOL"


async def test_an_invalid_submission_reports_its_errors():
    bad = {**_submission("record"), "timestamp": "not a time"}
    with pytest.raises(ToolError) as err:
        await _tools(_Recording())["record_decision"].handler({"submission": bad})
    assert err.value.payload["code"] == "ARGUMENTS_INVALID"
    assert ["timestamp"] in [e["loc"] for e in err.value.payload["errors"]]


async def test_a_missing_argument_is_reported():
    with pytest.raises(ToolError) as err:
        await _tools(_Recording())["lookup_decision"].handler({})
    assert "decision_id" in err.value.payload["detail"]


async def test_a_ledger_refusal_is_its_problem_document():
    refusal = LedgerError(403, "REASONER_MISMATCH", "wrong key")
    with pytest.raises(ToolError) as err:
        await _tools(_Recording(fail=refusal))["record_decision"].handler(
            {"submission": _submission("record")}
        )
    assert err.value.payload["code"] == "REASONER_MISMATCH"
    assert err.value.payload["status"] == 403


async def test_an_unreachable_ledger_is_a_tool_error():
    down = httpx.ConnectError("connection refused")
    with pytest.raises(ToolError) as err:
        await _tools(_Recording(fail=down))["lookup_decision"].handler(
            {"decision_id": _DECISION_ID}
        )
    assert err.value.payload["code"] == "LEDGER_UNAVAILABLE"


async def test_the_reading_tools_call_the_client():
    client = _Recording()
    tools = _tools(client)

    assert (await tools["lookup_decision"].handler({"decision_id": _DECISION_ID}))[
        "decision_id"
    ] == _DECISION_ID
    assert (await tools["replay_decision"].handler({"decision_id": _DECISION_ID}))[
        "matched"
    ] is True
    await tools["list_decisions_for_entity"].handler(
        {
            "entity_id": _ENTITY_ID,
            "limit": 5,
            "before": "2026-10-14T12:00:00+00:00",
            "before_id": _DECISION_ID,
        }
    )

    assert client.calls[0] == ("get_decision", _DECISION_ID)
    assert client.calls[1] == ("replay", _DECISION_ID)
    name, (entity_id, reasoner_id, limit, before, before_id) = client.calls[2]
    assert (name, entity_id, reasoner_id, limit, before_id) == (
        "entity_decisions",
        _ENTITY_ID,
        None,
        5,
        _DECISION_ID,
    )
    assert before == datetime(2026, 10, 14, 12, tzinfo=UTC)


async def test_resolve_hold_sends_the_resolution():
    client = _Recording()
    result = await _tools(client)["resolve_hold"].handler(
        {
            "decision_id": _DECISION_ID,
            "resolution_action": "BLOCK",
            "resolver_id": "analyst-7",
            "note": "confirmed fraud",
        }
    )
    [(name, (decision_id, resolution))] = client.calls
    assert (name, decision_id) == ("resolve", _DECISION_ID)
    assert resolution.resolution_action == DecisionAction.BLOCK
    assert result["realized_action"] == "BLOCK"
    with pytest.raises(ToolError):
        await _tools(client)["resolve_hold"].handler(
            {
                "decision_id": _DECISION_ID,
                "resolution_action": "HOLD",
                "resolver_id": "a",
                "note": "n",
            }
        )


async def test_the_tools_run_inside_the_server():
    server = McpServer(ledger_tools(_Recording()), name="t", version="1")
    reply = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "record_decision",
                "arguments": {"submission": _submission("record")},
            },
        }
    )
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["decision_action"] == "ALLOW"


async def test_an_unreachable_ledger_never_echoes_the_error_text():
    # An httpx message can quote a request header — the key is one.
    request = httpx.Request("GET", "http://ledger:8000/api/v1/decisions/x?y=1")
    leaky = httpx.LocalProtocolError("Illegal header value b'secret-key '")
    leaky.request = request
    with pytest.raises(ToolError) as err:
        await _tools(_Recording(fail=leaky))["lookup_decision"].handler(
            {"decision_id": _DECISION_ID}
        )
    detail = err.value.payload["detail"]
    assert "secret-key" not in detail
    assert "LocalProtocolError" in detail
    assert "GET http://ledger:8000/api/v1/decisions/x" in detail
    assert "y=1" not in detail
