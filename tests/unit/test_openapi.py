"""Unit tests for the OpenAPI engine and full Swagger coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sylve_mcp.openapi import OpenAPI, default_spec_path, shorten_def_name, slugify_path

SPEC = default_spec_path()


@pytest.fixture(scope="module")
def spec() -> OpenAPI:
    return OpenAPI(SPEC)


def test_bundled_spec_is_vendored() -> None:
    assert SPEC.exists()
    doc = json.loads(SPEC.read_text(encoding="utf-8"))
    assert doc["info"]["version"] == "0.3.1"


def test_spec_path_operation_counts(spec: OpenAPI) -> None:
    """The vendored spec must keep the exact path/operation counts we advertise."""
    assert len(spec.paths) == 336
    assert spec.operation_count() == 441


def test_every_operation_is_reachable_by_plan(spec: OpenAPI) -> None:
    """sylve_api_call can address every documented operation by template."""
    seen = set()
    for method, template, _op in spec.operations():
        resolved_method, resolved_template, _ = spec.plan({"method": method, "path": template})
        assert (resolved_method, resolved_template) == (method, template)
        seen.add(f"{method} {template}")
    assert len(seen) == 441


def test_concrete_paths_resolve_to_templates(spec: OpenAPI) -> None:
    cases = [
        ("GET", "/vm/101/stats", "/vm/{rid}/stats"),
        ("POST", "/jail/201/actions/start", "/jail/{ctid}/actions/{action}"),
        ("GET", "/zfs/pools/zroot/status", "/zfs/pools/{guid}/status"),
        ("DELETE", "/network/firewall/nat/5", "/network/firewall/nat/{id}"),
    ]
    for method, concrete, template in cases:
        _, resolved, _ = spec.plan({"method": method, "path": concrete})
        assert resolved == template


def test_plan_rejects_unknown_path_and_method(spec: OpenAPI) -> None:
    with pytest.raises(ValueError, match="not documented"):
        spec.plan({"method": "GET", "path": "/not/a/real/path"})
    with pytest.raises(ValueError, match="method must be"):
        spec.plan({"method": "BREW", "path": "/vm"})
    with pytest.raises(ValueError, match="not documented"):
        spec.plan({"method": "DELETE", "path": "/vm"})


def test_search_by_tag_method_and_query(spec: OpenAPI) -> None:
    vm_gets = spec.find(tag="VM", method="GET")
    assert vm_gets and all("VM" in item["tags"] for item in vm_gets)
    assert spec.find(tag="Jail", method="GET")
    assert spec.find(query="wireguard")
    assert spec.find(query="zzz-no-such-thing") == []


def test_search_respects_limit(spec: OpenAPI) -> None:
    assert len(spec.find(query="", tag="Network", limit=5)) == 5


def test_tag_summary_totals_all_operations(spec: OpenAPI) -> None:
    summary = spec.tags_summary()
    assert sum(item["operations"] for item in summary) == 441
    assert {item["tag"] for item in summary} >= {"VM", "Jail", "ZFS", "Network", "Cluster Backups"}


def test_operation_schema_resolves_refs(spec: OpenAPI) -> None:
    schema = spec.operation_schema("POST", "/vm")
    body = schema["request_body"]
    assert body["type"] == "object"
    for required in ("name", "rid", "cpuCores", "ram"):
        assert required in body["properties"]
    assert body["properties"]["vncPort"]["type"] == "integer"
    # $refs must be fully expanded into object schemas, not left as strings.
    cpu_pinning = body["properties"]["cpuPinning"]
    assert cpu_pinning["type"] == "array"
    assert cpu_pinning["items"].get("type") == "object"


def test_operation_schema_marks_path_params_required(spec: OpenAPI) -> None:
    schema = spec.operation_schema("POST", "/jail/{ctid}/actions/{action}")
    assert {p["name"] for p in schema["path_parameters"]} == {"ctid", "action"}
    assert all(p["required"] for p in schema["path_parameters"])


def test_query_parameters_listed(spec: OpenAPI) -> None:
    schema = spec.operation_schema("GET", "/cluster/backups/events")
    names = {p["name"] for p in schema["query_parameters"]}
    assert names == {"limit", "jobId", "nodeId"}


def test_dynamic_tools_include_every_operation(spec: OpenAPI) -> None:
    """Tools are built from the operations map, so all 441 are exposed."""
    assert len(spec.dynamic_tool_specs(None)) == 441


def test_dynamic_tools_filter_by_tag_case_insensitively(spec: OpenAPI) -> None:
    assert len(spec.dynamic_tool_specs({"vm"})) == 50
    assert len(spec.dynamic_tool_specs({"Jail"})) == 48
    assert len(spec.dynamic_tool_specs({"vm", "jail"})) == 98


def test_dynamic_tool_requires_first_path_param(spec: OpenAPI) -> None:
    tool = next(t for t in spec.dynamic_tool_specs({"Jail"}) if t["path"] == "/jail/{ctid}")
    assert tool["input_schema"]["required"] == ["ctid"]
    assert "body" not in tool["input_schema"]["properties"]
    post_jail = next(
        t
        for t in spec.dynamic_tool_specs({"Jail"})
        if t["path"] == "/jail" and t["name"].startswith("api_post_")
    )
    assert "body" in post_jail["input_schema"]["properties"]


def test_slugify_and_short_names() -> None:
    assert slugify_path("/vm/{rid}/actions/{action}") == "vm_rid_actions_action"
    assert (
        shorten_def_name(
            "github_com_alchemillahq_sylve_internal_interfaces_services_libvirt.CreateVMRequest"
        )
        == "libvirt.CreateVMRequest"
    )


def test_missing_spec_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sylve_mcp.openapi.default_spec_path", lambda: tmp_path / "missing.json")
    with pytest.raises(OSError):
        OpenAPI()
