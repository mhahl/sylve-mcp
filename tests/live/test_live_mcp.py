"""End-to-end read-only MCP tool calls against a live Sylve cluster.

Run with::

    export SYLVE_LIVE=1
    export SYLVE_SERVER=https://sylve.example.test:8181
    export SYLVE_TOKEN=eyJhbGciOi...
    make live-test

Never mutates the cluster: every case here is a GET.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.live.conftest import call_tool

pytestmark = pytest.mark.live

READ_ONLY_CASES: list[tuple[str, dict[str, Any]]] = [
    ("sylve_cluster_status", {}),
    ("sylve_cluster_resources", {}),
    ("sylve_node_list", {}),
    ("sylve_node_show", {}),
    ("sylve_node_audit_logs", {}),
    ("sylve_vm_list", {}),
    ("sylve_vm_list", {"detailed": True}),
    ("sylve_jail_list", {}),
    ("sylve_zfs_pool", {"action": "list"}),
    ("sylve_zfs_dataset", {"action": "list"}),
    ("sylve_zfs_snapshot", {"action": "list"}),
    ("sylve_download", {"action": "list"}),
    ("sylve_network_overview", {"kind": "interfaces"}),
    ("sylve_network_overview", {"kind": "switches"}),
    ("sylve_network_overview", {"kind": "routes"}),
    ("sylve_network_overview", {"kind": "dhcp"}),
    ("sylve_network_overview", {"kind": "firewall_traffic"}),
    ("sylve_network_overview", {"kind": "firewall_nat"}),
    ("sylve_replication", {"action": "policies"}),
    ("sylve_replication", {"action": "events"}),
    ("sylve_backup", {"action": "targets"}),
    ("sylve_backup", {"action": "jobs"}),
    ("sylve_backup", {"action": "events"}),
    ("sylve_task", {"action": "recent"}),
    ("sylve_task", {"action": "active"}),
    ("sylve_vm_template", {"action": "list"}),
    ("sylve_jail_template", {"action": "list"}),
    ("sylve_api_search", {"query": "wireguard", "limit": 5}),
]


def _skip_if(server: Any, tool: str, arguments: dict[str, Any], exc: Exception) -> None:
    message = str(exc).lower()
    for marker in ("not found", "not implemented", "401", "403"):
        if marker in message:
            pytest.skip(f"{tool} {arguments} unavailable on this server: {exc}")
    raise exc


@pytest.mark.parametrize(("tool", "arguments"), READ_ONLY_CASES, ids=lambda v: str(v)[:60])
def test_read_only_tool_against_live(
    live_server: Any, tool: str, arguments: dict[str, Any]
) -> None:
    """Every read-only tool must return JSON-serializable data from a live cluster."""
    try:
        result = call_tool(live_server, tool, arguments)
    except Exception as exc:  # noqa: BLE001 - _skip_if re-raises real failures
        _skip_if(live_server, tool, arguments, exc)
        return
    assert result is not None
    if tool != "sylve_api_search":
        assert not isinstance(result, str), f"{tool} returned a raw string instead of JSON"


def test_live_guest_detail_tools(live_server: Any) -> None:
    """Show/logs/templates for whatever guest happens to exist."""
    vms = call_tool(live_server, "sylve_vm_list", {})
    if vms:
        rid = vms[0]["rid"]
        assert call_tool(live_server, "sylve_vm_show", {"vm": rid})["rid"] == rid
        logs = call_tool(live_server, "sylve_vm_logs", {"vm": rid})
        assert "logs" in logs

    jails = call_tool(live_server, "sylve_jail_list", {})
    if jails:
        ctid = jails[0]["ctId"]
        detail = call_tool(live_server, "sylve_jail_show", {"jail": ctid})
        assert detail["ctId"] == ctid
    else:
        pytest.skip("no jails on this cluster")


def test_live_snapshot_listing(live_server: Any) -> None:
    vms = call_tool(live_server, "sylve_vm_list", {})
    if not vms:
        pytest.skip("no VMs on this cluster")
    snaps = call_tool(live_server, "sylve_vm_snapshot", {"action": "list", "vm": vms[0]["rid"]})
    assert isinstance(snaps, list)


def test_live_node_drain_dry_run(live_server: Any) -> None:
    """A dry-run drain plan is safe and should always return structure."""
    nodes = call_tool(live_server, "sylve_node_list", {})
    if not nodes:
        pytest.skip("no nodes reported")
    plan = call_tool(live_server, "sylve_node_drain", {"node": nodes[0]["hostname"]})
    assert plan["dry_run"] is True
    assert isinstance(plan["items"], list)


def test_live_api_schema_is_resolvable(live_server: Any) -> None:
    found = call_tool(live_server, "sylve_api_search", {"tag": "VM", "method": "POST"})
    assert found
    schema = call_tool(
        live_server, "sylve_api_schema", {"method": "POST", "path": found[0]["path"]}
    )
    assert schema["method"] == "POST"
    assert isinstance(schema["responses"], dict)


def test_live_api_call_reads_info(live_server: Any) -> None:
    try:
        result = call_tool(live_server, "sylve_api_call", {"method": "GET", "path": "/info/node"})
    except Exception as exc:  # noqa: BLE001 - skip on auth/endpoint unavailability
        _skip_if(live_server, "sylve_api_call", {"path": "/info/node"}, exc)
        return
    assert isinstance(result, dict)
    assert "hostname" in result
