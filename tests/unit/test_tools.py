"""Unit tests for curated Dev/Ops tools and the universal OpenAPI tools.

Tools are called directly (handlers are plain functions) with a mock HTTP
transport injected into ``sylve_mcp.tools._client_for``, so no network or live
server is required.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from sylve_mcp import tools
from sylve_mcp.client import SylveClient
from sylve_mcp.main import _parser, build_server
from sylve_mcp.openapi import OpenAPI
from sylve_mcp.protocol import MCPServer
from sylve_mcp.tools import MCPSettings


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


TOKEN = make_jwt(datetime.now(timezone.utc) + timedelta(hours=1))

NODES = [{"nodeUUID": "u1", "hostname": "node1.lan", "status": "online", "guestIDs": [101, 201]}]
RESOURCES = [
    {
        "hostname": "node1.lan",
        "nodeUUID": "u1",
        "vms": [{"rid": 101, "name": "web-01", "state": 1, "vncPort": 5900, "vncEnabled": True}],
        "jails": [{"ctId": 201, "name": "app-01", "state": "RUNNING", "type": "freebsd"}],
        "vmTemplates": [],
        "jailTemplates": [],
    }
]
POOLS = [
    {"name": "zroot", "pool_guid": "g-1", "state": "ONLINE", "size": 100, "free": 100 * 1024**4},
    {"name": "tank", "pool_guid": "g-2", "state": "ONLINE", "size": 100, "free": 100 * 1024**4},
]
SWITCHES = {"standard": [{"id": 1, "name": "lan0"}], "manual": []}
DOWNLOADS = [
    {
        "id": 1,
        "uuid": "11111111-1111-1111-1111-111111111111",
        "name": "debian.iso",
        "status": "complete",
    }
]


def default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api")
    method = request.method
    data: Any = None
    if path == "/cluster/nodes":
        data = NODES
    elif path == "/cluster/resources":
        data = RESOURCES
    elif path == "/cluster":
        data = {
            "cluster": {"enabled": True},
            "nodeId": "u1",
            "leaderId": "u1",
            "leaderAddress": "10.0.0.1",
            "nodes": [{"id": "u1", "address": "10.0.0.1", "isLeader": True}],
        }
    elif path == "/cluster/replication/policies":
        data = []
    elif path == "/cluster/backups/jobs":
        data = []
    elif path == "/info/node":
        data = {"hostname": "node1.lan", "logicalCores": 8, "sylveVersion": "0.3.1"}
    elif path == "/info/basic":
        data = {"hostname": "node1.lan", "sylveVersion": "0.3.1"}
    elif path in ("/info/cpu", "/info/ram", "/info/swap"):
        data = {"usage": 10}
    elif path == "/info/audit-records":
        data = [
            {"id": 1, "user": "admin", "action": json.dumps({"path": "/api/vm"}), "status": "200"}
        ]
    elif path == "/zfs/pools":
        data = POOLS
    elif path == "/zfs/pools/g-1/status":
        data = {"state": "ONLINE", "name": "zroot"}
    elif path == "/zfs/datasets":
        data = [{"guid": "d-1", "name": "zroot/data", "type": "FILESYSTEM"}]
    elif path == "/network/switch":
        data = SWITCHES
    elif path == "/utilities/downloads":
        data = (
            DOWNLOADS
            if request.method == "GET"
            else {"uuid": "22222222-2222-2222-2222-222222222222"}
        )
    elif path in ("/vm", "/vm/101"):
        data = (
            [
                {
                    "rid": 101,
                    "name": "web-01",
                    "state": 5,
                    "vncEnabled": True,
                    "vncPort": 5900,
                    "cpuSockets": 1,
                    "cpuCores": 2,
                    "cpuThreads": 2,
                    "ram": 1024**3,
                    "startAtBoot": False,
                }
            ]
            if path == "/vm"
            else {"rid": 101, "name": "web-01", "cpuCores": 2, "ram": 1024**3}
        )
    elif path == "/vm/101/guest-agent":
        data = {
            "interfaces": [
                {"name": "lo0", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
                {"name": "vtnet0", "ip-addresses": [{"ip-address": "10.0.0.50"}]},
            ]
        }
    elif path == "/vm/101/logs":
        data = {"logs": "boot ok"}
    elif path == "/vm/101/snapshots":
        data = [{"id": 3, "snapshotName": "snap1"}]
    elif path == "/vm/templates":
        data = [{"id": 5, "name": "golden", "sourceVmName": "web-01"}]
    elif path == "/vm/templates/5":
        data = {"id": 5, "name": "golden", "createdAt": "2026-01-01T00:00:00Z"}
    elif path == "/jail/201":
        data = {"ctId": 201, "name": "app-01", "cores": 2, "memory": 1024**3}
    elif path == "/jail/201/guest-info":
        data = {"os": "FreeBSD 14.2"}
    elif path == "/jail/201/snapshots":
        data = [{"id": 4, "snapshotName": "snap-a"}]
    elif path == "/jail/bootstraps":
        data = [{"name": "14.2-base", "pool": "zroot", "major": 14, "minor": 2}]
    elif path == "/jail/templates":
        data = [{"id": 6, "name": "base-jail", "sourceJailName": "app-01"}]
    elif path == "/jail/templates/6":
        data = {"id": 6, "name": "base-jail", "pool": "zroot"}
    elif path == "/tasks/lifecycle/recent":
        data = [{"id": 42, "status": "success", "guestType": "vm", "guestId": 101}]
    elif path == "/tasks/lifecycle/active":
        data = []
    elif path == "/tasks/lifecycle/42":
        data = {"id": 42, "status": "success"}
    elif path.startswith("/cluster/replication/events"):
        data = []
    elif path.startswith("/cluster/backups/events"):
        data = []
    elif path.startswith("/cluster/backups/targets"):
        data = []
    elif path == "/network/interface":
        data = [{"name": "em0"}]
    elif path == "/network/dhcp/config":
        data = {"enabled": True}
    elif path == "/network/dhcp/range":
        data = []
    elif path == "/network/dhcp/lease":
        data = []
    elif path == "/network/firewall/traffic/counters":
        data = []
    elif path == "/network/firewall/nat/counters":
        data = []
    elif path == "/system/ppt-devices":
        data = []
    elif path == "/utilities/downloads/storage":
        data = []
    elif method == "GET":
        data = {}
    else:
        data = {"taskId": 42}

    return httpx.Response(200, json={"status": "success", "data": data})


@contextmanager
def patched_tools(
    handler: Callable[[httpx.Request], httpx.Response] = default_handler,
    read_only: bool = False,
) -> Iterator[MCPSettings]:
    """Route every tool's client through a mock HTTP transport."""
    settings = MCPSettings(read_only=read_only)

    def _client_for(params: dict[str, Any], _settings: MCPSettings) -> SylveClient:
        cfg = tools.load_config(_settings.config_path)
        _, ctx = cfg.get_active_context(params.get("context") or _settings.context)
        client = SylveClient(
            ctx,
            node_override=params.get("node") or _settings.node,
            timeout=_settings.timeout,
            read_only=_settings.read_only,
        )
        client.close()
        client._http = httpx.Client(
            base_url=client._base_url, transport=httpx.MockTransport(handler)
        )
        return client

    original = tools._client_for
    tools._client_for = _client_for  # type: ignore[assignment]
    try:
        yield settings
    finally:
        tools._client_for = original  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "current_context: prod",
                "contexts:",
                "  prod:",
                "    server: https://sylve.example.test",
                "    username: admin",
                "    hostname: node1.lan",
                "    node_id: u1",
                f"    token: '{TOKEN}'",
                "    verify_ssl: false",
                "    default_node: node1.lan",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SYLVE_CONFIG", str(cfg))
    for name in ("SYLVE_TOKEN", "SYLVE_SERVER", "SYLVE_NODE", "SYLVE_CONTEXT", "SYLVE_INSECURE"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Session & cluster
# ---------------------------------------------------------------------------
def test_whoami_and_context_list() -> None:
    with patched_tools() as settings:
        info = tools._whoami({}, settings)
        contexts = tools._context_list({}, settings)
    assert info["context"] == "prod"
    assert info["server"] == "https://sylve.example.test"
    assert info["token_status"].startswith("VALID")
    assert contexts == [
        {
            "current": True,
            "name": "prod",
            "server": "https://sylve.example.test",
            "username": "admin",
            "auth_type": "sylve",
            "hostname": "node1.lan",
            "default_node": "node1.lan",
            "token_status": "VALID",
        }
    ]


def test_cluster_status_and_resources() -> None:
    with patched_tools() as settings:
        status = tools._cluster_status({}, settings)
        resources = tools._cluster_resources({}, settings)
    assert status["enabled"] is True
    assert status["nodes_online"] == "1/1"
    assert status["leader_id"] == "u1"
    assert resources[0]["hostname"] == "node1.lan"


def test_node_tools() -> None:
    with patched_tools() as settings:
        nodes = tools._node_list({}, settings)
        detail = tools._node_show({}, settings)
        audit = tools._node_audit_logs({}, settings)
    assert nodes[0]["hostname"] == "node1.lan"
    assert detail["hostname"] == "node1.lan"
    assert detail["cpu_details"] == {"usage": 10}
    assert audit[0]["requestPath"] == "/api/vm"


# ---------------------------------------------------------------------------
# Node drain
# ---------------------------------------------------------------------------
def test_node_drain_dry_run_default() -> None:
    with patched_tools() as settings:
        result = tools._node_drain({"node": "node1.lan"}, settings)
    assert result["dry_run"] is True
    assert result["source_node"] == "node1.lan"
    # Only node1 exists, so no migration target -> the guest is blocked.
    assert result["items"][0]["action"] == "blocked"


def test_node_drain_stop_only_plan() -> None:
    with patched_tools() as settings:
        result = tools._node_drain(
            {"node": "node1.lan", "stop_only": True, "dry_run": True}, settings
        )
    actions = {item["guest_type"]: item["action"] for item in result["items"]}
    assert actions["vm"] == "shutdown"
    assert actions["jail"] == "stop"


def test_node_drain_executes_stop_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    base = default_handler

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        calls.append((request.method, path))
        return base(request)

    with patched_tools(handler) as settings:
        result = tools._node_drain(
            {"node": "node1.lan", "stop_only": True, "dry_run": False, "wait": False},
            settings,
        )
    assert result["executed"] is True
    assert ("POST", "/vm/101/actions/shutdown") in calls
    assert ("POST", "/jail/201/actions/stop") in calls


def test_node_drain_requires_node() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="required"):
            tools._node_drain({}, settings)


def test_node_drain_blocks_when_preflight_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if path == "/tasks/migration/validate":
            return httpx.Response(
                200, json={"status": "success", "data": {"valid": False, "reason": "pool full"}}
            )
        if path == "/vm/101/migrations":
            raise AssertionError("must not migrate after blocked preflight")
        return default_handler(request)

    two_nodes = NODES + [{"nodeUUID": "u2", "hostname": "node2.lan", "status": "online"}]

    def with_targets(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if path == "/cluster/nodes":
            return httpx.Response(200, json={"status": "success", "data": two_nodes})
        return handler(request)

    with patched_tools(with_targets) as settings:
        with pytest.raises(tools.SylveError, match="preflight"):
            tools._node_drain({"node": "node1.lan", "dry_run": False}, settings)
        plan = tools._node_drain({"node": "node1.lan", "dry_run": True}, settings)
    assert plan["items"][0]["action"] == "blocked"
    assert "pool full" in plan["items"][0]["reason"]


# ---------------------------------------------------------------------------
# VMs
# ---------------------------------------------------------------------------
def test_vm_list_summary_and_detailed() -> None:
    with patched_tools() as settings:
        summary = tools._vm_list({}, settings)
        detailed = tools._vm_list({"detailed": True}, settings)
    assert summary[0]["state"] == "RUNNING"
    assert summary[0]["node"] == "node1.lan"
    assert detailed[0]["vcpus"] == 4
    assert detailed[0]["vnc_port"] == 5900


def test_vm_show_includes_guest_agent_ips() -> None:
    with patched_tools() as settings:
        data = tools._vm_show({"vm": "web-01"}, settings)
    assert data["rid"] == 101
    assert data["state_name"] == "RUNNING"


def test_vm_show_requires_identifier() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="required"):
            tools._vm_show({}, settings)


def test_vm_action_requires_valid_action() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="action must be"):
            tools._vm_action({"action": "detonate", "vms": ["web-01"]}, settings)
        with pytest.raises(tools.SylveError, match="non-empty list"):
            tools._vm_action({"action": "stop", "vms": []}, settings)


def test_vm_action_shutdown() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.removeprefix('/api')}")
        return default_handler(request)

    with patched_tools(handler) as settings:
        results = tools._vm_action(
            {"action": "shutdown", "vms": ["web-01"], "wait": False}, settings
        )
    assert results[0]["rid"] == 101
    assert "POST /vm/101/actions/shutdown" in seen


def test_vm_logs() -> None:
    with patched_tools() as settings:
        assert tools._vm_logs({"vm": "101"}, settings)["logs"] == "boot ok"


def test_vm_snapshots() -> None:
    with patched_tools() as settings:
        listed = tools._vm_snapshot({"action": "list", "vm": "web-01"}, settings)
        created = tools._vm_snapshot(
            {"action": "create", "vm": "web-01", "name": "snap2", "wait": False}, settings
        )
        with pytest.raises(tools.SylveError, match="snapshot_id"):
            tools._vm_snapshot({"action": "rollback", "vm": "web-01"}, settings)
        deleted = tools._vm_snapshot(
            {"action": "delete", "vm": "web-01", "snapshot_id": 3}, settings
        )
    assert listed[0]["snapshotName"] == "snap1"
    assert created["created"] is True
    assert deleted["deleted"] is True


def test_vm_templates_list_enriches_metadata() -> None:
    with patched_tools() as settings:
        rows = tools._vm_template({"action": "list"}, settings)
        shown = tools._vm_template({"action": "show", "template_id": 5}, settings)
        deleted = tools._vm_template({"action": "delete", "template_id": 5}, settings)
    assert rows[0]["createdAt"] == "2026-01-01T00:00:00Z"
    assert shown["name"] == "golden"
    assert deleted["deleted"] is True


def test_vm_template_requires_template_id() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="template_id"):
            tools._vm_template({"action": "show"}, settings)


# ---------------------------------------------------------------------------
# VM creation
# ---------------------------------------------------------------------------
def test_vm_create_dry_run_allocates_rid_and_vnc_port() -> None:
    with patched_tools() as settings:
        result = tools._vm_create(
            {
                "name": "new-01",
                "memory": "2G",
                "disk_size": "20G",
                "pool": "zroot",
                "dry_run": True,
            },
            settings,
        )
    payload = result["payload"]
    assert result["node"] == "node1.lan"
    assert payload["rid"] == 202
    assert payload["ram"] == 2 * 1024**3
    assert payload["storageSize"] == 20 * 1024**3
    assert payload["storagePool"] == "zroot"
    assert payload["vncPort"] == 5901
    assert len(payload["vncPassword"]) == 12
    assert payload["vncEnabled"] is True


def test_vm_create_rejects_bad_name_and_sizes() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="only letters"):
            tools._vm_create({"name": "bad name!", "pool": "zroot", "dry_run": True}, settings)
        with pytest.raises(tools.SylveError, match="greater than zero"):
            tools._vm_create(
                {"name": "ok", "pool": "zroot", "memory": "0G", "dry_run": True}, settings
            )
        with pytest.raises(tools.SylveError, match="not found"):
            tools._vm_create({"name": "ok", "pool": "nope", "dry_run": True}, settings)


def test_vm_create_requires_unambiguous_pool() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="Multiple ZFS pools"):
            tools._vm_create({"name": "ok", "dry_run": True}, settings)


def test_vm_create_cloud_init_validation() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="Cloud-init requires"):
            tools._vm_create(
                {"name": "ok", "pool": "zroot", "user_data": "x: 1", "dry_run": True}, settings
            )


def test_vm_create_executes_and_starts() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        seen.append(f"{request.method} {path}")
        if request.method == "POST" and path == "/vm":
            return httpx.Response(200, json={"status": "success", "data": {"rid": 202}})
        return default_handler(request)

    with patched_tools(handler) as settings:
        result = tools._vm_create(
            {"name": "new-02", "pool": "zroot", "vnc": False, "start": True}, settings
        )
    assert result["rid"] == 202
    assert result["started"] is True
    assert "POST /vm" in seen


def test_vm_migrate_validate_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if path == "/tasks/migration/validate":
            return httpx.Response(200, json={"status": "success", "data": {"valid": True}})
        return default_handler(request)

    with patched_tools(handler) as settings:
        result = tools._vm_migrate(
            {"vm": "web-01", "target": "node1.lan", "validate_only": True}, settings
        )
    assert result["preflight"] == {"valid": True}


def test_vm_template_pool_remap_uses_template_detail() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        seen.append(f"{request.method} {path}")
        if request.method == "POST" and path == "/vm/templates/5/vms":
            return httpx.Response(200, json={"status": "success", "data": {"taskId": 7}})
        if path == "/vm/templates/5":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "id": 5,
                        "name": "golden",
                        "storages": [
                            {"sourceStorageId": 11, "pool": "zroot"},
                            {"sourceStorageId": 12, "pool": "tank"},
                        ],
                    },
                },
            )
        return default_handler(request)

    with patched_tools(handler) as settings:
        result = tools._vm_create({"template": 5, "pool": "zroot", "dry_run": True}, settings)
    assert result["mode"] == "template"
    assert result["request"]["storagePools"] == [
        {"sourceStorageId": 11, "pool": "zroot"},
        {"sourceStorageId": 12, "pool": "zroot"},
    ]
    assert "GET /vm/templates/5" in seen


def test_vm_migrate_requires_args() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="required"):
            tools._vm_migrate({"vm": "web-01"}, settings)


def test_vm_delete_builds_query_flags() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return default_handler(request)

    with patched_tools(handler) as settings:
        result = tools._vm_delete({"vm": "web-01", "delete_volumes": False}, settings)
    assert result["deleted"] is True
    delete_req = next(r for r in seen if r.method == "DELETE")
    assert delete_req.url.params["deletevolumes"] == "false"


# ---------------------------------------------------------------------------
# Jails
# ---------------------------------------------------------------------------
def test_jail_list_and_show() -> None:
    with patched_tools() as settings:
        rows = tools._jail_list({}, settings)
        detail = tools._jail_show({"jail": "app-01"}, settings)
    assert rows[0]["ctId"] == 201
    assert detail["guest_info"] == {"os": "FreeBSD 14.2"}


def test_jail_action_validation() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="action must be"):
            tools._jail_action({"action": "boot", "jails": ["app-01"]}, settings)
        results = tools._jail_action(
            {"action": "restart", "jails": ["app-01"], "wait": False}, settings
        )
    assert results[0]["ctId"] == 201


def test_jail_create_defaults_ctid_and_sizes() -> None:
    with patched_tools() as settings:
        result = tools._jail_create(
            {"name": "new-jail", "pool": "zroot", "memory": "1G", "dhcp": True}, settings
        )
    assert result["ctId"] == 202
    assert result["created"] is True


def test_jail_create_requires_name_and_pool() -> None:
    with patched_tools() as settings:
        with pytest.raises(tools.SylveError, match="required"):
            tools._jail_create({"name": "x"}, settings)


def test_jail_logs_snapshots_bootstrap_templates() -> None:
    with patched_tools() as settings:
        logs = tools._jail_logs({"jail": "app-01"}, settings)
        snaps = tools._jail_snapshot({"action": "list", "jail": "app-01"}, settings)
        bts = tools._jail_bootstrap({"action": "list", "pool": "zroot"}, settings)
        tmpls = tools._jail_template({"action": "list"}, settings)
    assert "logs" in logs
    assert snaps[0]["id"] == 4
    assert bts[0]["name"] == "14.2-base"
    assert tmpls[0]["pool"] == "zroot"


def test_jail_migrate_and_delete() -> None:
    with patched_tools() as settings:
        deleted = tools._jail_delete({"jail": "app-01"}, settings)
        with pytest.raises(tools.SylveError, match="required"):
            tools._jail_migrate({"jail": "app-01"}, settings)
    assert deleted["ctId"] == 201


# ---------------------------------------------------------------------------
# ZFS, downloads, network
# ---------------------------------------------------------------------------
def test_zfs_pool_actions() -> None:
    with patched_tools() as settings:
        pools = tools._zfs_pool({"action": "list"}, settings)
        status = tools._zfs_pool({"action": "status", "pool": "zroot"}, settings)
        scrub = tools._zfs_pool({"action": "scrub", "pool": "zroot"}, settings)
        with pytest.raises(tools.SylveError, match="required"):
            tools._zfs_pool({"action": "status"}, settings)
    assert len(pools) == 2
    assert status["name"] == "zroot"
    assert scrub["scrub_started"] is True


def test_zfs_dataset_create_and_delete() -> None:
    with patched_tools() as settings:
        created = tools._zfs_dataset(
            {"action": "create_filesystem", "parent": "zroot", "name": "data"}, settings
        )
        vol = tools._zfs_dataset(
            {"action": "create_volume", "parent": "zroot", "name": "vol", "size": "10G"}, settings
        )
        deleted = tools._zfs_dataset(
            {"action": "delete", "dataset": "zroot/data", "type": "filesystem"}, settings
        )
    assert created["created"] is True
    assert vol["name"] == "vol"
    assert deleted["deleted"] is True


def test_zfs_snapshot_create_and_rollback() -> None:
    with patched_tools() as settings:
        created = tools._zfs_snapshot(
            {"action": "create", "dataset": "zroot/data", "name": "s1", "wait": False}, settings
        )
        rollback = tools._zfs_snapshot(
            {"action": "rollback", "snapshot": "zroot/data@s1", "wait": False}, settings
        )
        periodic = tools._zfs_snapshot({"action": "periodic_list"}, settings)
    assert created["snapshot"] == "zroot/data@s1"
    assert rollback["rolled_back"] is True
    assert periodic == []


def test_download_actions() -> None:
    with patched_tools() as settings:
        listed = tools._download({"action": "list"}, settings)
        shown = tools._download({"action": "show", "identifier": "debian.iso"}, settings)
        queued = tools._download({"action": "add", "url": "https://x/y.iso"}, settings)
        with pytest.raises(tools.SylveError, match="not found"):
            tools._download({"action": "show", "identifier": "missing"}, settings)
    assert listed[0]["uuid"].startswith("1111")
    assert shown["id"] == 1
    assert queued["queued"] is True


def test_network_overview_kinds() -> None:
    with patched_tools() as settings:
        interfaces = tools._network_overview({"kind": "interfaces"}, settings)
        dhcp = tools._network_overview({"kind": "dhcp"}, settings)
        counters = tools._network_overview({"kind": "firewall_counters"}, settings)
        with pytest.raises(tools.SylveError, match="kind must be"):
            tools._network_overview({"kind": "bogus"}, settings)
    assert interfaces == [{"name": "em0"}]
    assert dhcp["config"] == {"enabled": True}
    assert counters == {"traffic": [], "nat": []}


def test_replication_and_backup_actions() -> None:
    with patched_tools() as settings:
        policies = tools._replication({"action": "policies"}, settings)
        jobs = tools._backup({"action": "jobs"}, settings)
        with pytest.raises(tools.SylveError, match="policy_id"):
            tools._replication({"action": "failover"}, settings)
        with pytest.raises(tools.SylveError, match="action must be"):
            tools._backup({"action": "nuke"}, settings)
    assert policies == []
    assert jobs == []


def test_task_actions() -> None:
    with patched_tools() as settings:
        recent = tools._task({"action": "recent"}, settings)
        shown = tools._task({"action": "show", "task_id": 42}, settings)
        with pytest.raises(tools.SylveError, match="task_id"):
            tools._task({"action": "show"}, settings)
    assert recent[0]["id"] == 42
    assert shown["status"] == "success"


# ---------------------------------------------------------------------------
# Universal OpenAPI tools
# ---------------------------------------------------------------------------
def test_api_search_and_schema() -> None:
    spec = OpenAPI()
    with patched_tools() as settings:
        found = tools._api_search({"query": "wireguard", "limit": 3}, settings, spec)
        schema = tools._api_schema({"method": "POST", "path": "/jail"}, settings, spec)
        with pytest.raises(tools.SylveError, match="required"):
            tools._api_schema({}, settings, spec)
    assert found and all("wireguard" in json.dumps(item).lower() for item in found)
    assert {"name", "pool", "type", "ctId"} <= set(schema["request_body"]["properties"])


def test_api_call_substitutes_path_and_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.removeprefix('/api')}")
        return default_handler(request)

    with patched_tools(handler) as settings:
        result = tools._api_call(
            {
                "method": "GET",
                "path": "/vm/{rid}/stats",
                "path_params": {"rid": 101},
                "query_params": {"step": None},
            },
            settings,
            OpenAPI(),
        )
        assert result == {}
        with pytest.raises(tools.SylveError, match="Missing path parameter"):
            tools._api_call({"method": "GET", "path": "/vm/{rid}/stats"}, settings, OpenAPI())
    assert "GET /vm/101/stats" in seen


def test_api_call_is_blocked_in_read_only_mode() -> None:
    with patched_tools(read_only=True) as settings:
        with pytest.raises(tools.SylveError, match="read-only"):
            tools._api_call(
                {"method": "DELETE", "path": "/vm/{rid}", "path_params": {"rid": 101}},
                settings,
                OpenAPI(),
            )


def test_api_call_download_streams_to_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"binary-bytes")

    target = tmp_path / "file.bin"
    with patched_tools(handler) as settings:
        result = tools._api_call(
            {
                "method": "GET",
                "path": "/utilities/downloads/{uuid}",
                "path_params": {"uuid": "abc"},
                "download_path": str(target),
            },
            settings,
            OpenAPI(),
        )
    assert result["path"] == str(target)
    assert target.read_bytes() == b"binary-bytes"


def test_api_call_from_descriptor_splits_params() -> None:
    spec = OpenAPI()
    desc = next(
        d for d in spec.dynamic_tool_specs({"jail"}) if d["path"] == "/jail/{ctid}/actions/{action}"
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return default_handler(request)

    with patched_tools(handler) as settings:
        tools._api_call_from_descriptor(
            desc, {"ctid": 201, "action": "start", "wait": False}, settings, spec
        )
    assert seen[-1].url.path == "/api/jail/201/actions/start"


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------
def _server(tags: str | None = None, expose_all: bool = False) -> MCPServer:
    args = _parser().parse_args(
        (["--expose-tags", tags] if tags else []) + (["--expose-all"] if expose_all else [])
    )
    return build_server(MCPSettings(), args)


def test_server_registers_curated_and_openapi_tools() -> None:
    server = _server()
    names = set(server._tools)
    assert len([n for n in names if n.startswith("sylve_")]) == 38
    assert {"sylve_api_search", "sylve_api_schema", "sylve_api_call"} <= names
    assert {"sylve_vm_create", "sylve_jail_action", "sylve_node_drain"} <= names
    assert "sylve://cluster/status" in server._resources
    assert {"triage-cluster", "plan-node-drain"} <= set(server._prompts)


def test_every_registered_tool_has_schema_and_annotations() -> None:
    server = _server()
    for name, spec in server._tools.items():
        assert spec["description"], name
        assert spec["inputSchema"]["type"] == "object", name
        assert "node" in spec["inputSchema"]["properties"], name
        assert "context" in spec["inputSchema"]["properties"], name
        assert "readOnlyHint" in spec.get("annotations", {}), name


def test_dynamic_tools_can_be_exposed_by_tag() -> None:
    server = _server(tags="Tasks")
    assert len(server._tools) == 38 + 6
    assert "api_get_tasks_lifecycle_recent" in server._tools


def test_expose_all_registers_every_operation() -> None:
    server = _server(expose_all=True)
    assert len(server._tools) == 38 + 441


def test_read_only_annotation_on_read_tools() -> None:
    server = _server()
    for name in ("sylve_vm_list", "sylve_cluster_status", "sylve_task"):
        assert server._tools[name]["annotations"]["readOnlyHint"] is (name != "sylve_task")
    for name in ("sylve_vm_delete", "sylve_jail_delete", "sylve_node_drain"):
        assert server._tools[name]["annotations"]["destructiveHint"] is True


def test_tool_call_over_protocol_returns_json() -> None:
    import io

    with patched_tools():
        server = _server()
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "sylve_whoami", "arguments": {}},
                }
            )
            + "\n"
        )
        stdout = io.StringIO()
        server.run(stdin, stdout)
    response = json.loads(stdout.getvalue())
    assert response["result"]["isError"] is False
    assert json.loads(response["result"]["content"][0]["text"])["context"] == "prod"
