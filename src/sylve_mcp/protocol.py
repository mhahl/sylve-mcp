"""Minimal Model Context Protocol (MCP) server over stdio.

The MCP stdio transport is newline-delimited JSON-RPC 2.0: one JSON request or
notification per line on stdin, one response per line on stdout. Everything in
this module is stdlib so the package only needs ``httpx`` and ``PyYAML`` at
runtime (matching the sylve-cli philosophy of avoiding extra dependencies).

Supported methods: initialize, ping, tools/list, tools/call, resources/list,
resources/read, prompts/list, prompts/get. Notifications are acknowledged by
being ignored.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from typing import Any, TextIO

JSONRPC = "2.0"
PROTOCOL_VERSION = "2025-03-26"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
INVALID_RESOURCE = -32002


class MCPError(Exception):
    """JSON-RPC error to return to the client instead of an internal failure."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPServer:
    """A stdio MCP server with registered tools, resources, and prompts."""

    def __init__(self, server_info: dict[str, str]) -> None:
        self.server_info = server_info
        self._tools: dict[str, dict[str, Any]] = {}
        self._resources: dict[str, dict[str, Any]] = {}
        self._prompts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
        annotations: dict[str, Any] | None = None,
    ) -> None:
        spec: dict[str, Any] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "handler": handler,
        }
        if annotations:
            spec["annotations"] = annotations
        self._tools[name] = spec

    def register_resource(
        self,
        uri: str,
        name: str,
        loader: Callable[[], str],
        description: str = "",
        mime_type: str = "application/json",
    ) -> None:
        self._resources[uri] = {
            "uri": uri,
            "name": name,
            "description": description,
            "mimeType": mime_type,
            "loader": loader,
        }

    def register_prompt(
        self,
        name: str,
        description: str,
        loader: Callable[[dict[str, Any]], str],
    ) -> None:
        self._prompts[name] = {"name": name, "description": description, "loader": loader}

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------
    def _tool_public(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in spec.items() if k != "handler"}

    def _handle(self, method: str, params: Any) -> Any:
        params = params or {}

        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "logging": {},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": False, "subscribe": False},
                    "tools": {"listChanged": False},
                },
                "serverInfo": self.server_info,
                "instructions": (
                    "You are managing a Sylve FreeBSD hypervisor cluster. Use the "
                    "sylve_* tools. For any API operation not covered by a dedicated tool, "
                    "use sylve_api_search to find it, sylve_api_schema to inspect its "
                    "request/response shape, and sylve_api_call to run it. Destructive "
                    "operations will be confirmed by the user before execution."
                ),
            }

        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": [self._tool_public(s) for s in self._tools.values()]}

        if method == "tools/call":
            name = str(params.get("name") or "")
            spec = self._tools.get(name)
            if spec is None:
                raise MCPError(METHOD_NOT_FOUND, f"Unknown tool: {name}")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise MCPError(INVALID_PARAMS, "Tool arguments must be an object.")
            try:
                result = spec["handler"](args)
            except MCPError as exc:
                return {
                    "content": [{"type": "text", "text": f"Error: {exc.message}"}],
                    "isError": True,
                }
            except Exception as exc:  # noqa: BLE001 - surface any handler failure to the model
                return {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                }
            text = result if isinstance(result, str) else _to_json(result)
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if method == "resources/list":
            return {
                "resources": [
                    {k: v for k, v in spec.items() if k != "loader"}
                    for spec in self._resources.values()
                ]
            }

        if method == "resources/read":
            uri = str(params.get("uri") or "")
            spec = self._resources.get(uri)
            if spec is None:
                raise MCPError(INVALID_RESOURCE, f"Resource not found: {uri}", {"uri": uri})
            content: dict[str, Any] = {"uri": uri, "text": spec["loader"]()}
            if spec.get("mimeType"):
                content["mimeType"] = spec["mimeType"]
            return {"contents": [content]}

        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": spec["name"],
                        "description": spec["description"],
                    }
                    for spec in self._prompts.values()
                ]
            }

        if method == "prompts/get":
            name = str(params.get("name") or "")
            spec = self._prompts.get(name)
            if spec is None:
                raise MCPError(METHOD_NOT_FOUND, f"Unknown prompt: {name}")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            text = spec["loader"](args)
            return {
                "description": spec["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }

        if method in (
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/roots/list_changed",
            "notifications/tools/list_changed",
        ):
            return None

        raise MCPError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _process(self, message: Any) -> tuple[Any, dict[str, Any] | None]:
        """Return ``(result, error)`` for one JSON-RPC message."""
        if not isinstance(message, dict) or message.get("jsonrpc") != JSONRPC:
            return None, {
                "code": INVALID_REQUEST,
                "message": "Invalid Request",
            }
        method = message.get("method")
        if not isinstance(method, str):
            return None, {"code": INVALID_REQUEST, "message": "Invalid method"}
        try:
            result = self._handle(method, message.get("params"))
        except MCPError as exc:
            return None, {"code": exc.code, "message": exc.message, "data": exc.data}
        except Exception as exc:  # noqa: BLE001
            return None, {"code": INTERNAL_ERROR, "message": f"Internal error: {exc}"}
        if method.startswith("notifications/"):
            return None, None
        return result, None

    # ------------------------------------------------------------------
    # I/O loop
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_messages(stream: TextIO) -> Iterator[tuple[Any, str | None]]:
        """Yield ``(message, parse_error)`` per non-empty stdin line."""
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line), None
            except json.JSONDecodeError as exc:
                yield None, f"Parse error: {exc}"

    def run(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        """Serve on stdin/stdout until EOF. Returns the process exit status."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for message, parse_error in self._iter_messages(stdin):
            if parse_error:
                self._write(
                    stdout,
                    {
                        "jsonrpc": JSONRPC,
                        "id": None,
                        "error": {"code": PARSE_ERROR, "message": parse_error},
                    },
                )
                continue
            if "id" not in message:
                # Notification: still dispatch so state (e.g. initialized) settles.
                try:
                    self._handle(message.get("method") or "", message.get("params"))
                except Exception:  # noqa: BLE001
                    pass
                continue
            response: dict[str, Any] = {"jsonrpc": JSONRPC, "id": message.get("id")}
            result, error = self._process(message)
            if error is not None:
                response["error"] = error
            else:
                response["result"] = result
            self._write(stdout, response)
        return 0

    @staticmethod
    def _write(stdout: TextIO, payload: dict[str, Any]) -> None:
        stdout.write(json.dumps(payload, default=str) + "\n")
        stdout.flush()


def _to_json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
