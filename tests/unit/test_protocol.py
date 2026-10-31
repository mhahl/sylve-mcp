"""Unit tests for the stdio MCP JSON-RPC 2.0 server."""

from __future__ import annotations

import io
import json

import pytest

from sylve_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    INVALID_RESOURCE,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    MCPServer,
)


@pytest.fixture()
def server() -> MCPServer:
    s = MCPServer({"name": "test", "version": "9.9"})
    s.register_tool(
        "echo",
        "Echo the arguments.",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        lambda params: {"echo": params.get("text")},
        {"readOnlyHint": True},
    )
    s.register_tool("boom", "Raise.", {"type": "object"}, lambda params: 1 / 0)
    s.register_resource("test://a", "A", lambda: '{"ok": true}', description="d")
    s.register_prompt("greet", "Greet.", lambda args: f"hi {args.get('name', 'there')}")
    return s


def drive(server: MCPServer, messages: list[object]) -> list[dict]:
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    stdout = io.StringIO()
    assert server.run(stdin, stdout) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_initialize_advertises_capabilities(server: MCPServer) -> None:
    [resp] = drive(server, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"] == {"name": "test", "version": "9.9"}
    assert set(result["capabilities"]) == {"logging", "prompts", "resources", "tools"}


def test_tools_list_includes_annotations(server: MCPServer) -> None:
    [resp] = drive(server, [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert tools["echo"]["annotations"]["readOnlyHint"] is True
    assert tools["echo"]["inputSchema"]["type"] == "object"
    assert "handler" not in tools["echo"]


def test_tools_call_returns_text_content(server: MCPServer) -> None:
    [resp] = drive(
        server,
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hello"}},
            }
        ],
    )
    assert resp["result"]["isError"] is False
    assert json.loads(resp["result"]["content"][0]["text"]) == {"echo": "hello"}


def test_tools_call_handler_failure_is_reported_not_fatal(server: MCPServer) -> None:
    responses = drive(
        server,
        [
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "boom"}},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        ],
    )
    assert responses[0]["result"]["isError"] is True
    assert "division by zero" in responses[0]["result"]["content"][0]["text"]
    assert responses[1]["result"] == {}


def test_unknown_tool_and_method_return_errors(server: MCPServer) -> None:
    responses = drive(
        server,
        [
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/bogus"},
        ],
    )
    assert responses[0]["error"]["code"] == METHOD_NOT_FOUND
    assert responses[1]["error"]["code"] == METHOD_NOT_FOUND


def test_notifications_get_no_response(server: MCPServer) -> None:
    assert drive(server, [{"jsonrpc": "2.0", "method": "notifications/initialized"}]) == []


def test_parse_error_and_invalid_request(server: MCPServer) -> None:
    stdin = io.StringIO('{not json}\n{"id": 8, "method": "ping"}\n')
    stdout = io.StringIO()
    server.run(stdin, stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == PARSE_ERROR
    assert lines[1]["error"]["code"] == INVALID_REQUEST


def test_resources_list_and_read(server: MCPServer) -> None:
    responses = drive(
        server,
        [
            {"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 10, "method": "resources/read", "params": {"uri": "test://a"}},
            {"jsonrpc": "2.0", "id": 11, "method": "resources/read", "params": {"uri": "test://z"}},
        ],
    )
    assert responses[0]["result"]["resources"][0]["uri"] == "test://a"
    assert "loader" not in responses[0]["result"]["resources"][0]
    assert responses[1]["result"]["contents"][0]["text"] == '{"ok": true}'
    assert responses[2]["error"]["code"] == INVALID_RESOURCE


def test_prompts_list_and_get(server: MCPServer) -> None:
    responses = drive(
        server,
        [
            {"jsonrpc": "2.0", "id": 12, "method": "prompts/list"},
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "prompts/get",
                "params": {"name": "greet", "arguments": {"name": "ada"}},
            },
        ],
    )
    assert responses[0]["result"]["prompts"][0]["name"] == "greet"
    message = responses[1]["result"]["messages"][0]
    assert message["role"] == "user"
    assert message["content"]["text"] == "hi ada"


def test_internal_error_is_wrapped() -> None:
    s = MCPServer({"name": "test", "version": "0"})

    def explode(method: str, params: object) -> object:
        raise RuntimeError("kaboom")

    s._handle = explode  # type: ignore[method-assign]
    [resp] = drive(s, [{"jsonrpc": "2.0", "id": 14, "method": "ping"}])
    assert resp["error"]["code"] == INTERNAL_ERROR
    assert "kaboom" in resp["error"]["message"]
