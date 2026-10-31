"""Unit tests for the Sylve client: auth, routing, read-only, and phantom-route guards."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sylve_mcp.client import (
    ResourceNotFoundError,
    SessionExpiredError,
    SylveAPIError,
    SylveClient,
    SylveError,
)
from sylve_mcp.config import ContextConfig


def make_jwt(expires_at: datetime) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "admin", "exp": int(expires_at.timestamp())}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.sig"


def make_context(**overrides: object) -> ContextConfig:
    defaults: dict[str, object] = {
        "server": "https://sylve.example.test",
        "token": make_jwt(datetime.now(timezone.utc) + timedelta(hours=1)),
        "verify_ssl": False,
    }
    defaults.update(overrides)
    return ContextConfig(**defaults)  # type: ignore[arg-type]


@contextmanager
def client_with(
    handler: Callable[[httpx.Request], httpx.Response],
    context: ContextConfig | None = None,
    **kwargs: object,
) -> Iterator[SylveClient]:
    client = SylveClient(context or make_context(), **kwargs)  # type: ignore[arg-type]
    client.close()
    client._http = httpx.Client(base_url=client._base_url, transport=httpx.MockTransport(handler))
    try:
        yield client
    finally:
        client.close()


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


# ---------------------------------------------------------------------------
# Auth contract
# ---------------------------------------------------------------------------
def test_missing_token_raises_session_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent without a token")

    with client_with(handler, context=make_context(token="")) as client:
        with pytest.raises(SessionExpiredError, match="No authentication token"):
            client.get("/vm/1")


def test_401_raises_session_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "error", "message": "nope"})

    with client_with(handler) as client:
        with pytest.raises(SessionExpiredError, match="HTTP 401"):
            client.get("/vm/1")


def test_bearer_and_node_headers_are_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        seen["host"] = request.headers.get("X-Current-Hostname", "")
        if request.url.path == "/api/cluster/nodes":
            return _ok([{"nodeUUID": "u1", "hostname": "node1.lan", "status": "online"}])
        return _ok([])

    with client_with(handler, node_override="node1.lan") as client:
        client.get("/vm")
    assert seen["auth"].startswith("Bearer ")
    assert seen["host"] == "node1.lan"


# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------
def test_read_only_refuses_non_get_before_io() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("read-only mode must not send a request")

    with client_with(handler, read_only=True) as client:
        with pytest.raises(SylveError, match="read-only"):
            client.post("/vm/1/actions/stop")
        with pytest.raises(SylveError, match="read-only"):
            client.delete("/vm/1")


def test_read_only_allows_get() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cluster/nodes":
            return _ok([])
        return _ok([{"rid": 1}])

    with client_with(handler, read_only=True) as client:
        assert client.get("/vm") == [{"rid": 1}]


def test_unsupported_method_rejected() -> None:
    with client_with(lambda r: _ok(None)) as client:
        with pytest.raises(SylveError, match="Unsupported HTTP method"):
            client.request("TRACE", "/vm")


# ---------------------------------------------------------------------------
# Error and phantom-route contracts
# ---------------------------------------------------------------------------
def test_html_catch_all_is_reported_as_not_implemented() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><body><div id='app'></div></body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    with client_with(handler) as client:
        with pytest.raises(SylveAPIError, match="not implemented"):
            client.get("/network/interface/l3")


def test_non_json_error_body_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            content=b"<html><body>Not Found</body></html>",
            headers={"Content-Type": "text/html"},
        )

    with client_with(handler) as client:
        with pytest.raises(SylveAPIError) as exc_info:
            client.get("/vm/9")
    assert exc_info.value.status_code == 404
    assert "Not Found" in str(exc_info.value)


def test_error_envelope_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "error": "vm_not_found", "message": "no such vm"},
        )

    with client_with(handler) as client:
        with pytest.raises(SylveAPIError, match="no such vm") as exc_info:
            client.get("/vm/1")
    assert exc_info.value.error_code == "vm_not_found"


def test_data_envelope_is_unwrapped() -> None:
    with client_with(lambda r: _ok({"rid": 7})) as client:
        assert client.get("/vm/7") == {"rid": 7}


def test_connection_error_becomes_sylve_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with client_with(handler) as client:
        with pytest.raises(SylveError, match="Connection error"):
            client.get("/vm")


# ---------------------------------------------------------------------------
# Node discovery and guest resolution invariants
# ---------------------------------------------------------------------------
def test_cluster_nodes_fall_back_to_info_node() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cluster/nodes":
            return httpx.Response(500, json={"status": "error", "message": "boom"})
        assert request.url.path == "/api/info/node"
        return _ok({"hostname": "standalone.lan", "logicalCores": 4})

    with client_with(handler) as client:
        nodes = client.get_cluster_nodes()
    assert [n["hostname"] for n in nodes] == ["standalone.lan"]


def test_resolve_vm_by_name_and_ambiguous_name_raises() -> None:
    resources = [
        {"hostname": "node1.lan", "vms": [{"rid": 101, "name": "web"}], "jails": []},
        {"hostname": "node2.lan", "vms": [{"rid": 202, "name": "web"}], "jails": []},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cluster/resources":
            return _ok(resources)
        if request.url.path == "/api/cluster/nodes":
            return _ok([])
        return _ok([])

    with client_with(handler) as client:
        with pytest.raises(SylveError, match="Multiple VMs match"):
            client.resolve_vm("web")

    multi = [{"hostname": "n1", "vms": [{"rid": 5, "name": "web"}], "jails": []}]

    def handler_one(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cluster/resources":
            return _ok(multi)
        return _ok([])

    with client_with(handler_one) as client:
        rid, host, summary = client.resolve_vm("web")
    assert (rid, host, summary["name"]) == (5, "n1", "web")


def test_resolve_jail_not_found_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cluster/resources":
            return _ok([{"hostname": "n1", "vms": [], "jails": []}])
        return _ok([])

    with client_with(handler) as client:
        with pytest.raises(ResourceNotFoundError, match="not found"):
            client.resolve_jail("missing")


def test_resolve_node_uuid_unknown_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cluster/nodes":
            return _ok([{"nodeUUID": "u1", "hostname": "n1"}])
        return _ok([])

    with client_with(handler) as client:
        with pytest.raises(ResourceNotFoundError, match="ghost"):
            client.resolve_node_uuid("ghost")
        assert client.resolve_node_uuid("n1") == ("u1", "n1")


def test_resolve_pool_guid_by_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/zfs/pools":
            return _ok([{"name": "zroot", "pool_guid": "g-1"}])
        return _ok([])

    with client_with(handler) as client:
        guid, _host = client.resolve_pool_guid("zroot")
    assert guid == "g-1"
