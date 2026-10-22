"""A minimal Model Context Protocol server: JSON-RPC 2.0 over stdio, tools only.

Hand-rolled on purpose (plan section 9): the ledger exposes seven tools and
nothing else (no resources, prompts, or sampling), so the server is the MCP
lifecycle plus ``tools/list`` and ``tools/call``, with no SDK dependency.

The transport is newline-delimited JSON on stdin/stdout: each message is
one line, and nothing but protocol messages is ever written to stdout (logs
go to stderr). Requests are handled concurrently — a slow gated decision
does not hold up a ``ping`` — and every response is written whole, under a
lock.

Lifecycle and methods:

- ``initialize``: the server answers with the protocol version the client
  asked for when it supports it, else its latest; its capabilities
  (``tools``); its name and version; and instructions for the agent.
- ``notifications/initialized`` and other notifications: no response.
- ``ping``: an empty result.
- ``tools/list``: every tool's name, title, description, ``inputSchema``,
  and annotations.
- ``tools/call``: an unknown tool is a JSON-RPC error (``-32602``); anything
  the tool itself reports — invalid arguments, a refusal from the ledger —
  is a result with ``isError: true`` and the details as JSON text and
  ``structuredContent``, so an agent can read it and correct itself.
- anything else: ``-32601`` (method not found); a line that is not JSON:
  ``-32700``; a message that is not a JSON-RPC request: ``-32600``. A JSON
  array (a batch, from protocol version 2025-03-26) is answered with an
  array.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

#: Protocol versions this server speaks, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool's own failure, reported to the agent as an ``isError`` result.

    Args:
        payload: JSON-shaped details (a problem document, a validation
            error list) the agent reads to correct itself.
    """

    def __init__(self, payload: dict[str, Any]) -> None:  # noqa: D107
        self.payload = payload
        super().__init__(json.dumps(payload))


@dataclass(frozen=True)
class Tool:
    """One MCP tool: its definition and the coroutine that runs it.

    Args:
        name: Unique tool name.
        title: Human-readable title.
        description: What the tool does, written for an agent.
        input_schema: JSON Schema of the ``arguments`` object.
        handler: ``arguments -> JSON result``; raises ``ToolError`` for a
            failure the agent should see.
        annotations: MCP tool annotations (``readOnlyHint``, ...).
    """

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    annotations: dict[str, Any] = field(default_factory=dict)

    def definition(self) -> dict[str, Any]:
        """The tool as ``tools/list`` describes it."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, **self.annotations},
        }


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_result(payload: Any, *, is_error: bool) -> dict[str, Any]:
    text = json.dumps(payload, indent=2, default=str)
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


class McpServer:
    """An MCP server over a fixed set of tools.

    Args:
        tools: The tools it serves.
        name: Server name reported at ``initialize``.
        version: Server version reported at ``initialize``.
        instructions: Guidance for the agent, reported at ``initialize``.
    """

    def __init__(
        self,
        tools: Sequence[Tool],
        *,
        name: str,
        version: str,
        instructions: str = "",
    ) -> None:
        """Index the tools by name."""
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            msg = "tool names must be unique"
            raise ValueError(msg)
        self._name = name
        self._version = version
        self._instructions = instructions

    @property
    def tools(self) -> list[Tool]:
        """The served tools, in registration order."""
        return list(self._tools.values())

    async def handle(self, message: Any) -> Any:
        """Answer one JSON-RPC message (or a batch); ``None`` for notifications."""
        if isinstance(message, list):
            if not message:
                return _error(None, INVALID_REQUEST, "empty batch")
            answers = [await self._handle_one(item) for item in message]
            replies = [a for a in answers if a is not None]
            return replies or None
        return await self._handle_one(message)

    async def _handle_one(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if not isinstance(method, str):
            return (
                None
                if "id" not in message
                else _error(message["id"], INVALID_REQUEST, "a request needs a method")
            )
        if "id" not in message:
            return None  # a notification: initialized, cancelled, ...
        request_id = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                return _result(request_id, self._initialize(params))
            if method == "ping":
                return _result(request_id, {})
            if method == "tools/list":
                return _result(
                    request_id, {"tools": [t.definition() for t in self.tools]}
                )
            if method == "tools/call":
                return await self._call(request_id, params)
        except Exception as err:  # never let one request kill the server
            return _error(request_id, INTERNAL_ERROR, f"{type(err).__name__}: {err}")
        return _error(request_id, METHOD_NOT_FOUND, f"method {method!r} not found")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self._name, "version": self._version},
        }
        if self._instructions:
            result["instructions"] = self._instructions
        return result

    async def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(request_id, INVALID_PARAMS, f"unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, INVALID_PARAMS, "arguments must be an object")
        try:
            payload = await tool.handler(arguments)
        except ToolError as err:
            return _result(request_id, _tool_result(err.payload, is_error=True))
        return _result(request_id, _tool_result(payload, is_error=False))

    async def serve(
        self,
        read_line: Callable[[], Awaitable[bytes]],
        write_line: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """Serve until the input ends; answer each line concurrently.

        Args:
            read_line: Returns the next line (``b""`` at end of input).
            write_line: Writes one line (the server adds no newline of its
                own beyond the one it appends to each message).
        """
        lock = asyncio.Lock()
        pending: set[asyncio.Task[None]] = set()

        async def _answer(line: bytes) -> None:
            try:
                message = json.loads(line)
            except ValueError:
                reply: Any = _error(None, PARSE_ERROR, "the line is not JSON")
            else:
                reply = await self.handle(message)
            if reply is not None:
                data = json.dumps(reply, separators=(",", ":"), default=str)
                async with lock:
                    await write_line(data.encode("utf-8") + b"\n")

        while True:
            line = await read_line()
            if not line:
                break
            if not line.strip():
                continue
            task = asyncio.create_task(_answer(line))
            pending.add(task)
            task.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending)


async def serve_stdio(server: McpServer) -> None:
    """Serve ``server`` on this process's stdin and stdout."""
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    async def _read() -> bytes:
        return await asyncio.to_thread(stdin.readline)

    async def _write(data: bytes) -> None:
        stdout.write(data)
        stdout.flush()

    await server.serve(_read, _write)
