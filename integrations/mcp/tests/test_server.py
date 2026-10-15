"""The hand-rolled MCP server speaks the protocol's lifecycle and tool calls.

- ``initialize`` answers the client's protocol version when supported, else
  the server's latest, with the tools capability, server info, and the
  agent instructions; notifications get no response; ``ping`` answers.
- ``tools/list`` describes every tool; ``tools/call`` returns the tool's
  JSON as text and ``structuredContent``; a tool's own failure is an
  ``isError`` result; an unknown tool, an unknown method, a non-JSON line,
  and a non-JSON-RPC message are JSON-RPC errors.
- A batch is answered with an array; requests are served concurrently and
  every response is one line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from decisionledger_mcp.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpServer,
    Tool,
    ToolError,
)


async def _echo(arguments: dict[str, Any]) -> Any:
    if arguments.get("fail"):
        raise ToolError({"code": "NOPE", "detail": "asked to fail"})
    if arguments.get("sleep"):
        await asyncio.sleep(arguments["sleep"])
    return {"echo": arguments}


ECHO = Tool(
    name="echo",
    title="Echo",
    description="Echo the arguments.",
    input_schema={"type": "object"},
    handler=_echo,
    annotations={"readOnlyHint": True},
)


@pytest.fixture
def server() -> McpServer:
    return McpServer([ECHO], name="test", version="9.9", instructions="be nice")


def _request(method: str, params: dict[str, Any] | None = None, id_: int = 1):
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return message


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
async def test_initialize_answers_a_supported_version(server: McpServer, version):
    reply = await server.handle(_request("initialize", {"protocolVersion": version}))
    result = reply["result"]
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "test", "version": "9.9"}
    assert result["instructions"] == "be nice"


async def test_initialize_answers_its_latest_for_an_unknown_version(server):
    reply = await server.handle(_request("initialize", {"protocolVersion": "1999"}))
    assert reply["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]


async def test_notifications_get_no_response_and_ping_answers(server):
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert await server.handle(notification) is None
    assert (await server.handle(_request("ping")))["result"] == {}


async def test_tools_list_describes_every_tool(server):
    [tool] = (await server.handle(_request("tools/list")))["result"]["tools"]
    assert tool == {
        "name": "echo",
        "title": "Echo",
        "description": "Echo the arguments.",
        "inputSchema": {"type": "object"},
        "annotations": {"title": "Echo", "readOnlyHint": True},
    }


async def test_a_tool_call_returns_text_and_structured_content(server):
    reply = await server.handle(
        _request("tools/call", {"name": "echo", "arguments": {"x": 1}})
    )
    result = reply["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"echo": {"x": 1}}
    assert json.loads(result["content"][0]["text"]) == {"echo": {"x": 1}}
    assert result["content"][0]["type"] == "text"


async def test_a_tools_own_failure_is_an_error_result(server):
    reply = await server.handle(
        _request("tools/call", {"name": "echo", "arguments": {"fail": True}})
    )
    assert reply["result"]["isError"] is True
    assert reply["result"]["structuredContent"]["code"] == "NOPE"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (_request("tools/call", {"name": "nope"}), INVALID_PARAMS),
        (_request("tools/call", {"name": "echo", "arguments": [1]}), INVALID_PARAMS),
        (_request("resources/list"), METHOD_NOT_FOUND),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, INVALID_REQUEST),
        (_request("ping", params=None) | {"params": [1]}, INVALID_PARAMS),
    ],
)
async def test_protocol_errors_are_jsonrpc_errors(server, message, code):
    reply = await server.handle(message)
    assert reply["error"]["code"] == code


async def test_a_batch_is_answered_with_an_array(server):
    replies = await server.handle(
        [
            _request("ping", id_=1),
            {"jsonrpc": "2.0", "method": "x"},
            _request("ping", id_=2),
        ]
    )
    assert [r["id"] for r in replies] == [1, 2]


def _line(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode() + b"\n"


async def _serve(server: McpServer, lines: list[bytes]) -> list[dict[str, Any]]:
    queue = list(lines)
    written: list[bytes] = []

    async def _read() -> bytes:
        await asyncio.sleep(0)
        return queue.pop(0) if queue else b""

    async def _write(data: bytes) -> None:
        written.append(data)

    await server.serve(_read, _write)
    assert all(w.endswith(b"\n") and w.count(b"\n") == 1 for w in written)
    return [json.loads(w) for w in written]


async def test_serving_answers_each_line_and_reports_parse_errors(server):
    replies = await _serve(
        server,
        [
            json.dumps(
                _request("initialize", {"protocolVersion": "2025-06-18"})
            ).encode()
            + b"\n",
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            b"\n",
            b"{not json\n",
            json.dumps(_request("tools/list", id_=2)).encode() + b"\n",
        ],
    )
    by_id = {r["id"]: r for r in replies}
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert by_id[None]["error"]["code"] == PARSE_ERROR
    assert len(by_id[2]["result"]["tools"]) == 1


async def test_a_slow_call_does_not_hold_up_a_ping(server):
    replies = await _serve(
        server,
        [
            json.dumps(
                _request("tools/call", {"name": "echo", "arguments": {"sleep": 0.2}})
            ).encode()
            + b"\n",
            json.dumps(_request("ping", id_=2)).encode() + b"\n",
        ],
    )
    assert [r["id"] for r in replies] == [2, 1]


def test_tool_names_are_unique():
    with pytest.raises(ValueError, match="unique"):
        McpServer([ECHO, ECHO], name="x", version="1")
