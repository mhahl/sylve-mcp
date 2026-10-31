"""Fixtures for live tests against a real Sylve cluster."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from sylve_mcp.client import SylveClient, SylveError
from sylve_mcp.config import ContextConfig, normalize_server_url
from sylve_mcp.main import _parser, build_server
from sylve_mcp.tools import MCPSettings

ROOT = Path(__file__).resolve().parents[2]


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture(scope="session")
def live_settings() -> dict[str, str]:
    """Return live connection settings or skip the live suite."""
    if not _truthy("SYLVE_LIVE"):
        pytest.skip("live tests are disabled; set SYLVE_LIVE=1")
    server = os.environ.get("SYLVE_SERVER", "")
    token = os.environ.get("SYLVE_TOKEN", "")
    if not server or not token:
        pytest.skip("live tests need SYLVE_SERVER and SYLVE_TOKEN")
    return {
        "server": server,
        "token": token,
        "node": os.environ.get("SYLVE_NODE", ""),
        "insecure": os.environ.get("SYLVE_INSECURE", ""),
    }


@pytest.fixture(scope="session")
def live_client(live_settings: dict[str, str]) -> Any:
    """Return a direct client for the live target."""
    context = ContextConfig(
        server=normalize_server_url(live_settings["server"]),
        token=live_settings["token"],
        verify_ssl=not _truthy("SYLVE_INSECURE"),
        default_node=live_settings["node"] or None,
    )
    client = SylveClient(context, node_override=live_settings["node"] or None)
    if client.context.is_token_expired():
        pytest.fail("SYLVE_TOKEN is expired")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def live_server(live_settings: dict[str, str]) -> Any:
    """Build the full MCP server pointed at the live cluster."""
    settings = MCPSettings(
        node=live_settings["node"] or None,
        insecure=_truthy(live_settings["insecure"]),
    )
    return build_server(settings, _parser().parse_args([]))


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke one tool over the JSON-RPC layer and parse its JSON text result."""
    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        + "\n"
    )
    stdout = io.StringIO()
    server.run(stdin, stdout)
    response = json.loads(stdout.getvalue())
    result = response["result"]
    if result["isError"]:
        raise SylveError(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])
