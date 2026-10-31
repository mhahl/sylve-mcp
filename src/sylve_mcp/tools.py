"""Curated cluster-aware MCP tools plus the universal OpenAPI tools.

Every handler returns JSON-serializable data. Read hints, destructive hints,
and idempotency hints are declared per tool so MCP clients can auto-grant safe
queries and prompt before state changes.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sylve_mcp.client import SylveAPIError, SylveClient, SylveError
from sylve_mcp.config import decode_jwt_payload, load_config
from sylve_mcp.openapi import OpenAPI
from sylve_mcp.protocol import MCPServer
from sylve_mcp.tasks import (
    get_lifecycle_task,
    wait_for_backup_event,
    wait_for_lifecycle_task,
    wait_for_replication_event,
)

Handler = Callable[[dict[str, Any], "MCPSettings"], Any]

_VM_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SIZE_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]*)\s*$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
    "p": 1024**5,
    "pb": 1024**5,
    "pib": 1024**5,
}

_SCRATCH_ALIASES = {
    "cores": "cpuCores",
    "cpu": "cpuCores",
    "sockets": "cpuSockets",
    "threads": "cpuThreads",
    "memory": "ram",
    "tpm": "tpmEmulation",
    "qemu_guest_agent": "qemuGuestAgent",
    "start_at_boot": "startAtBoot",
    "start_order": "startOrder",
    "time_offset": "timeOffset",
    "boot_rom": "bootRom",
    "extra_bhyve_options": "extraBhyveOptions",
    "pci_devices": "pciDevices",
    "storage_type": "storageType",
    "storage_pool": "storagePool",
    "pool": "storagePool",
    "disk_size": "storageSize",
    "storage_size": "storageSize",
    "storage_emulation": "storageEmulationType",
    "switch_name": "switchName",
    "switch": "switchName",
    "switch_emulation": "switchEmulationType",
    "mac_object": "macId",
    "mac_id": "macId",
    "vnc_enabled": "vncEnabled",
    "vnc_bind": "vncBind",
    "vnc_port": "vncPort",
    "vnc_password": "vncPassword",
    "vnc_resolution": "vncResolution",
    "vnc_wait": "vncWait",
    "cloud_init": "cloudInit",
    "user_data": "cloudInitData",
    "meta_data": "cloudInitMetaData",
    "network_config": "cloudInitNetworkConfig",
}

_TEMPLATE_ALIASES = {
    "template_id": "template",
    "templateId": "template",
    "start_rid": "startRid",
    "name_prefix": "namePrefix",
    "storage_pools": "storagePools",
    "rewrite_cloud_init_identity": "rewriteCloudInitIdentity",
    "cloud_init_prefix": "cloudInitPrefix",
}


@dataclass
class MCPSettings:
    """Server-level configuration applied to every tool call."""

    config_path: Path | None = None
    context: str | None = None
    node: str | None = None
    insecure: bool = False
    timeout: float = 30.0
    read_only: bool = False
    swagger: Path | None = None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _g(params: dict[str, Any], key: str, default: Any = None) -> Any:
    return params.get(key, default)


def _bool(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    return bool(value)


def _client_for(params: dict[str, Any], settings: MCPSettings) -> SylveClient:
    cfg = load_config(settings.config_path)
    _, ctx = cfg.get_active_context(_g(params, "context") or settings.context)
    if settings.insecure:
        ctx.verify_ssl = False
    node = _g(params, "node") or settings.node
    return SylveClient(
        ctx,
        node_override=node,
        timeout=settings.timeout,
        read_only=settings.read_only,
    )


def _format_vm_state(state: Any) -> str:
    if isinstance(state, str) and not state.isdigit():
        return state.strip().upper()
    mapping = {
        0: "NOSTATE",
        1: "RUNNING",
        2: "BLOCKED",
        3: "PAUSED",
        4: "SHUTDOWN",
        5: "SHUTOFF",
        6: "CRASHED",
        7: "PMSUSPENDED",
    }
    try:
        return mapping.get(int(state), "UNKNOWN")
    except (TypeError, ValueError):
        return "UNKNOWN"


def _format_bytes(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return str(value or "")
    if size <= 0:
        return "0"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size = int(size / 1024)
    return str(value)


def _parse_size(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SylveError(f"Invalid {field}: {value!r}. Use a size like 2G or 512M.")
    if isinstance(value, (int, float)):
        size = int(value)
    else:
        text = str(value).strip()
        match = _SIZE_RE.match(text)
        if match is None:
            raise SylveError(f"Invalid {field}: '{value}'. Use a size like 2G or 512M.")
        number = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit not in _SIZE_UNITS:
            raise SylveError(f"Invalid {field} unit '{unit}'. Use B, K, M, G, or T.")
        size = int(number * _SIZE_UNITS[unit])
    if size <= 0:
        raise SylveError(f"{field} must be greater than zero.")
    return size


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SylveError(f"Invalid {field}: {value!r}.") from exc


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise SylveError(f"Invalid {field}: {value!r}.")


def _read_text_value(value: Any, field: str) -> str:
    if value is None:
        return ""
    text = str(value)
    candidate = Path(text).expanduser()
    looks_like_path = text.startswith((".", "/", "~")) or text.lower().endswith((".yaml", ".yml"))
    if "\n" not in text and candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise SylveError(f"Could not read {field} from '{candidate}': {exc}") from exc
    if looks_like_path and "\n" not in text:
        raise SylveError(f"{field} file '{candidate}' does not exist.")
    return text


def _validate_yaml(text: str, field: str) -> None:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SylveError(f"{field} is not valid YAML: {exc}") from exc
    if parsed is None:
        raise SylveError(f"{field} is empty.")


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _maybe_wait(
    client: SylveClient,
    resp: Any,
    params: dict[str, Any],
    *,
    default_wait: bool = True,
) -> Any:
    """Poll a returned ``taskId`` or ``eventId`` to a bounded timeout."""
    wait = _bool(params, "wait", default_wait)
    timeout = float(_g(params, "wait_timeout", 60.0))
    node = _g(params, "node")
    if not wait or resp is None:
        return resp
    if not isinstance(resp, dict):
        return resp
    out = dict(resp)
    task_id = out.get("taskId") or 0
    if task_id not in (None, "", 0):
        out["task"] = wait_for_lifecycle_task(client, int(task_id), node=node, timeout=timeout)
    event_id = out.get("eventId") or 0
    if event_id not in (None, "", 0) and _g(params, "event_kind"):
        kind = str(_g(params, "event_kind"))
        if kind == "replication":
            wait_for_replication_event(client, int(event_id), node_id=node, timeout=timeout)
        elif kind == "backup":
            wait_for_backup_event(client, int(event_id), node_id=node, timeout=timeout)
        out["event"] = {"id": int(event_id), "polled": True}
    return out


def _resolve_host(client: SylveClient, node: str | None) -> str:
    host = client.resolve_node_hostname(node)
    if not host:
        host = client.context.hostname or ""
    if not host:
        nodes = client.get_online_nodes()
        if len(nodes) == 1:
            host = str(nodes[0].get("hostname") or "")
    if not host:
        raise SylveError("No target node selected. Pass 'node'.")
    return host


def _next_guest_id(client: SylveClient) -> int:
    max_id = 0
    try:
        for node in client.get_cluster_nodes():
            for guest_id in node.get("guestIDs") or []:
                try:
                    max_id = max(max_id, int(guest_id))
                except (TypeError, ValueError):
                    continue
    except SylveError:
        pass
    if max_id == 0:
        try:
            for node_res in client.get_cluster_resources():
                for guest in node_res.get("vms") or []:
                    if isinstance(guest, dict) and guest.get("rid") is not None:
                        max_id = max(max_id, int(guest["rid"]))
                for guest in node_res.get("jails") or []:
                    if isinstance(guest, dict) and guest.get("ctId") is not None:
                        max_id = max(max_id, int(guest["ctId"]))
        except SylveError:
            pass
    next_id = 100 if max_id < 100 else max_id + 1
    if next_id > 9999:
        raise SylveError("No free guest IDs remain in the 1-9999 range. Pass 'rid' explicitly.")
    return next_id


def _resolve_pool(client: SylveClient, host: str, requested: Any) -> tuple[str, dict[str, Any]]:
    pools: list[dict[str, Any]] = []
    try:
        data = client.get("/zfs/pools", node=host)
        if isinstance(data, list):
            pools = [p for p in data if isinstance(p, dict) and p.get("name")]
    except SylveError:
        pools = []
    if not pools:
        raise SylveError(f"No usable ZFS pools found on '{host}'.")
    if requested:
        wanted = str(requested).strip().lower()
        for pool in pools:
            if str(pool.get("name") or "").lower() == wanted:
                return str(pool["name"]), pool
        names = ", ".join(str(pool.get("name")) for pool in pools)
        raise SylveError(f"ZFS pool '{requested}' not found on '{host}'. Available: {names}.")
    if len(pools) == 1:
        pool = pools[0]
        return str(pool["name"]), pool
    names = ", ".join(str(pool.get("name")) for pool in pools)
    raise SylveError(
        f"Multiple ZFS pools are available on '{host}'; pass 'pool'. Available: {names}."
    )


def _list_switches(client: SylveClient, host: str) -> list[str]:
    try:
        data = client.get("/network/switch", node=host)
    except SylveError:
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    for key in ("standard", "manual"):
        for switch in data.get(key) or []:
            if isinstance(switch, dict) and switch.get("name"):
                names.append(str(switch["name"]))
    return names


def _resolve_switch(client: SylveClient, host: str, requested: Any) -> str:
    switches = _list_switches(client, host)
    wanted = str(requested).strip().lower()
    for name in switches:
        if name.lower() == wanted:
            return name
    available = ", ".join(switches) if switches else "none"
    raise SylveError(f"Network switch '{requested}' not found on '{host}'. Available: {available}.")


def _resolve_iso(client: SylveClient, host: str, requested: Any) -> str | None:
    if requested is None:
        return None
    ident = str(requested).strip()
    if not ident or ident.lower() == "none":
        return None
    downloads: Any = []
    try:
        downloads = client.get("/utilities/downloads", node=host)
    except SylveError:
        pass
    ident_lower = ident.lower()
    for item in downloads if isinstance(downloads, list) else []:
        if not isinstance(item, dict):
            continue
        values = {
            str(item.get("uuid") or "").lower(),
            str(item.get("name") or "").lower(),
            str(item.get("id") or "").lower(),
        }
        if ident_lower in values:
            uuid = str(item.get("uuid") or "")
            if not uuid:
                raise SylveError(f"Download '{item.get('name')}' has no UUID.")
            return uuid
    if _UUID_RE.fullmatch(ident):
        return ident
    names = [
        str(item.get("name") or item.get("uuid") or "")
        for item in (downloads if isinstance(downloads, list) else [])
        if isinstance(item, dict)
    ]
    available = ", ".join(name for name in names if name) or "none"
    raise SylveError(f"ISO/download '{requested}' not found on '{host}'. Available: {available}.")


def _resolve_mac_id(client: SylveClient, host: str, requested: Any) -> int | None:
    if requested is None or str(requested).strip() == "":
        return None
    text = str(requested).strip()
    if text.isdigit():
        return int(text)
    objects: Any = []
    try:
        objects = client.get("/network/object", node=host)
    except SylveError:
        pass
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict):
            continue
        if str(obj.get("type") or "").lower() != "mac":
            continue
        if str(obj.get("name") or "").lower() == text.lower():
            if obj.get("id") is None:
                break
            return int(obj["id"])
    raise SylveError(f"MAC object '{requested}' not found on '{host}'.")


def _resolve_pci_devices(client: SylveClient, host: str, requested: list[str] | None) -> list[int]:
    if not requested:
        return []
    devices: Any = []
    try:
        devices = client.get("/system/ppt-devices", node=host)
    except SylveError:
        pass
    by_name: dict[str, int] = {}
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, dict) or device.get("id") is None:
            continue
        device_id = int(device["id"])
        for key in ("deviceID", "deviceId", "name"):
            key_text = str(device.get(key) or "").strip().lower()
            if key_text:
                by_name[key_text] = device_id
    result: list[int] = []
    for item in requested:
        text = str(item).strip()
        if not text:
            continue
        if text.isdigit():
            pci_id = int(text)
        else:
            pci_id = by_name.get(text.lower(), 0)
            if pci_id == 0:
                raise SylveError(f"PCI passthrough device '{item}' not found on '{host}'.")
        if pci_id not in result:
            result.append(pci_id)
    return result


def _next_vnc_port(client: SylveClient, host: str) -> int:
    used: set[int] = set()
    vms: Any = []
    try:
        vms = client.get("/vm", node=host)
    except SylveError:
        pass
    for vm in vms if isinstance(vms, list) else []:
        if not isinstance(vm, dict) or vm.get("vncEnabled") is False:
            continue
        port = vm.get("vncPort", vm.get("vnc_port"))
        try:
            if port:
                used.add(int(port))
        except (TypeError, ValueError):
            continue
    for port in range(5900, 6000):
        if port not in used:
            return port
    raise SylveError("No free VNC port found in the 5900-5999 range; pass 'vnc_port'.")


def _generate_vnc_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _validate_vnc_bind(value: str) -> None:
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise SylveError(
            f"Invalid VNC bind address '{value}'. Use an IP like 127.0.0.1 or 0.0.0.0."
        ) from exc


def _friendly_create_error(exc: SylveAPIError) -> str:
    combined = f"{exc.error_code} {str(exc)}".lower()
    mappings = (
        ("rid_or_name_already_in_use", "RID or VM name is already in use."),
        ("guest_id_already_in_use", "RID is already used by another guest."),
        ("template_not_found", "The selected VM template was not found."),
        ("template_has_no_cloneable_storage", "The template has no cloneable storage."),
        ("invalid_rid_range", "The template RID range is invalid."),
        ("insufficient_pool_space", "Not enough free space in the selected pool."),
        ("pool_not_found", "The selected storage pool was not found."),
        ("storage_size_greater_than_available", "Not enough free space in the pool."),
        ("memory_must_be_greater_than_128mb", "Memory must be at least 128 MiB."),
        ("image_not_resolvable", "The selected ISO/image could not be resolved."),
        ("media_not_cloud_init_capable", "The selected ISO is not a cloud-init image."),
        ("cloud_init_requires_iso", "Cloud-init requires an ISO."),
        ("cloud_init_requires_storage", "Cloud-init requires a primary disk."),
        ("invalid_cloud_init_yaml", "Cloud-init user-data or meta-data is invalid YAML."),
        ("vnc_port_already_in_use_by_another_vm", "That VNC port is already used by another VM."),
        ("vnc_port_already_in_use_by_another_service", "That VNC port is already in use."),
        ("switch_not_found", "The selected network switch was not found."),
        ("no_switch_emulation_type_selected", "A network emulation type is required."),
        ("no_emulation_type_selected", "A storage emulation type is required."),
        ("no_pool_selected_for", "A storage pool is required."),
    )
    for code, message in mappings:
        if code in combined:
            return message
    return str(exc) or "VM creation failed."


def _normalize_scratch_payload(
    client: SylveClient,
    host: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name or not _VM_NAME_RE.fullmatch(name):
        raise SylveError("VM names may contain only letters, numbers, '-' and '_'.")
    payload["name"] = name

    rid = payload.get("rid")
    if rid in (None, "", 0, "0"):
        rid = _next_guest_id(client)
    rid = _as_int(rid, "RID")
    if not 1 <= rid <= 9999:
        raise SylveError("RID must be between 1 and 9999.")
    payload["rid"] = rid

    description = str(payload.get("description") or "")
    if description:
        payload["description"] = description
    else:
        payload.pop("description", None)

    for key, default in (("cpuSockets", 1), ("cpuCores", 1), ("cpuThreads", 1)):
        value = _as_int(payload.get(key, default), key)
        if value < 1:
            raise SylveError(f"{key} must be at least 1.")
        payload[key] = value

    ram = _parse_size(payload.get("ram", 1024**3), "memory")
    if ram < 128 * 1024**2:
        raise SylveError("Memory must be at least 128 MiB.")
    payload["ram"] = ram

    payload["timeOffset"] = str(payload.get("timeOffset") or "utc").strip().lower()
    payload["bootRom"] = str(payload.get("bootRom") or "uefi").strip().lower()

    storage_type = str(payload.get("storageType") or "zvol").strip().lower()
    payload["storageType"] = storage_type
    pool: dict[str, Any] | None = None
    if storage_type != "none":
        pool_name, pool = _resolve_pool(client, host, payload.get("storagePool"))
        payload["storagePool"] = pool_name
        size = _parse_size(payload.get("storageSize", 1024**3), "disk size")
        if size < 128 * 1024**2:
            raise SylveError("Disk size must be at least 128 MiB.")
        if pool.get("free") is not None and size > int(pool["free"]):
            raise SylveError(
                f"Not enough free space in pool '{pool_name}' "
                f"({_format_bytes(pool.get('free'))} available)."
            )
        payload["storageSize"] = size
        payload["storageEmulationType"] = (
            str(payload.get("storageEmulationType") or "virtio-blk").strip().lower()
        )
    else:
        for key in ("storagePool", "storageSize", "storageEmulationType"):
            payload.pop(key, None)

    iso = _resolve_iso(client, host, payload.get("iso"))
    if iso:
        payload["iso"] = iso
    else:
        payload.pop("iso", None)

    switch_name = payload.get("switchName")
    if switch_name is None or str(switch_name).strip().lower() in ("", "none"):
        for key in ("switchName", "switchEmulationType", "macId"):
            payload.pop(key, None)
    else:
        payload["switchName"] = _resolve_switch(client, host, switch_name)
        emulation = str(payload.get("switchEmulationType") or "").strip()
        if not emulation:
            raise SylveError("A network emulation type is required.")
        payload["switchEmulationType"] = emulation
        mac_id = _resolve_mac_id(client, host, payload.get("macId"))
        if mac_id is not None:
            payload["macId"] = mac_id
        else:
            payload.pop("macId", None)

    vnc_enabled = payload.get("vncEnabled")
    vnc_enabled = True if vnc_enabled is None else _as_bool(vnc_enabled, "vncEnabled")
    if vnc_enabled:
        bind = str(payload.get("vncBind") or "127.0.0.1").strip()
        _validate_vnc_bind(bind)
        payload["vncBind"] = bind
        port = payload.get("vncPort")
        if port in (None, "", 0, "0"):
            port = _next_vnc_port(client, host)
        port = _as_int(port, "vncPort")
        if not 1 <= port <= 65535:
            raise SylveError("VNC port must be between 1 and 65535.")
        payload["vncPort"] = port
        payload["vncResolution"] = str(payload.get("vncResolution") or "640x480").strip()
        password = str(payload.get("vncPassword") or _generate_vnc_password())
        if "," in password:
            raise SylveError("VNC passwords cannot contain commas.")
        payload["vncPassword"] = password
        payload["vncWait"] = _as_bool(payload.get("vncWait", False), "vncWait")
        payload["vncEnabled"] = True
    else:
        payload["vncEnabled"] = False
        for key in ("vncBind", "vncPort", "vncPassword", "vncResolution", "vncWait"):
            payload.pop(key, None)

    cloud_init = _as_bool(payload.get("cloudInit", False), "cloudInit")
    user_data = _read_text_value(payload.get("cloudInitData"), "cloud-init user-data")
    meta_data = _read_text_value(payload.get("cloudInitMetaData"), "cloud-init meta-data")
    network_config = _read_text_value(
        payload.get("cloudInitNetworkConfig"), "cloud-init network-config"
    )
    if not cloud_init and (user_data or meta_data or network_config):
        cloud_init = True
    if cloud_init:
        if storage_type == "none":
            raise SylveError("Cloud-init requires a primary disk.")
        if not payload.get("iso"):
            raise SylveError("Cloud-init requires 'iso' with a cloud-init capable image.")
        if not user_data or not meta_data:
            raise SylveError("Cloud-init requires both user-data and meta-data.")
        _validate_yaml(user_data, "cloud-init user-data")
        _validate_yaml(meta_data, "cloud-init meta-data")
        if network_config:
            _validate_yaml(network_config, "cloud-init network-config")
        payload["cloudInit"] = True
        payload["cloudInitData"] = user_data
        payload["cloudInitMetaData"] = meta_data
        if network_config:
            payload["cloudInitNetworkConfig"] = network_config
        else:
            payload.pop("cloudInitNetworkConfig", None)
    else:
        payload["cloudInit"] = False
        for key in ("cloudInitData", "cloudInitMetaData", "cloudInitNetworkConfig"):
            payload.pop(key, None)

    for key, default in (
        ("serial", False),
        ("tpmEmulation", False),
        ("qemuGuestAgent", False),
        ("ignoreUMSR", False),
        ("apic", True),
        ("acpi", True),
        ("startAtBoot", False),
    ):
        payload[key] = _as_bool(payload.get(key, default), key)

    payload["startOrder"] = _as_int(payload.get("startOrder", 0), "startOrder")
    payload["pciDevices"] = _resolve_pci_devices(client, host, payload.get("pciDevices"))

    extra = payload.get("extraBhyveOptions")
    if extra is None:
        payload["extraBhyveOptions"] = []
    elif isinstance(extra, list):
        payload["extraBhyveOptions"] = [str(item) for item in extra if str(item).strip()]
    else:
        payload["extraBhyveOptions"] = [str(extra)] if str(extra).strip() else []

    payload.setdefault("cpuPinning", [])
    return {key: value for key, value in payload.items() if value is not None}


def _resolve_template(
    client: SylveClient,
    host: str,
    identifier: Any,
) -> tuple[int, str]:
    text = str(identifier).strip()
    if text.isdigit():
        return int(text), text
    templates: Any = []
    try:
        templates = client.get("/vm/templates", node=host)
    except SylveError:
        pass
    for template in templates if isinstance(templates, list) else []:
        if not isinstance(template, dict):
            continue
        name = str(template.get("name") or "")
        if name.lower() == text.lower() and template.get("id") is not None:
            return int(template["id"]), name
    names = [
        str(template.get("name") or template.get("id") or "")
        for template in (templates if isinstance(templates, list) else [])
        if isinstance(template, dict)
    ]
    available = ", ".join(name for name in names if name) or "none"
    raise SylveError(f"VM template '{identifier}' not found on '{host}'. Available: {available}.")


def _template_storage_mappings(
    client: SylveClient,
    host: str,
    template_id: int,
    detail: dict[str, Any],
    requested_pool: Any,
    storage_pools: Any,
) -> list[dict[str, Any]]:
    """Map template clone storage to target pools.

    ``storage_pools`` may be "SOURCE_ID=POOL" strings or objects; a bare
    ``requested_pool`` applies to every cloneable storage reported by the
    template detail endpoint.
    """
    requested: dict[int, str] = {}
    if storage_pools:
        items = storage_pools if isinstance(storage_pools, list) else [storage_pools]
        for item in items:
            if isinstance(item, str):
                if "=" not in item:
                    raise SylveError("storage_pools strings must use SOURCE_ID=POOL.")
                src_text, dest_pool = item.split("=", 1)
                requested[_as_int(src_text, "sourceStorageId")] = dest_pool.strip()
            elif isinstance(item, dict):
                src_id = item.get("sourceStorageId", item.get("source_storage_id", item.get("id")))
                pool_value = item.get("pool")
                if src_id is None or not pool_value:
                    raise SylveError("storage_pools entries need sourceStorageId and pool.")
                requested[_as_int(src_id, "sourceStorageId")] = str(pool_value)

    detail_storages: list[Any] = []
    raw_storages = detail.get("storages")
    if isinstance(raw_storages, list):
        detail_storages = raw_storages
    source_ids: list[int] = []
    for storage in detail_storages:
        if not isinstance(storage, dict):
            continue
        src_id = storage.get("sourceStorageId", storage.get("id"))
        if src_id is not None:
            source_ids.append(_as_int(src_id, "sourceStorageId"))

    if requested_pool is not None:
        if not source_ids:
            raise SylveError(
                "Template has no cloneable storage metadata; pool cannot be remapped. "
                "Use storage_pools with explicit SOURCE_ID=POOL entries, or omit the pool."
            )
        for src_id in source_ids:
            requested.setdefault(src_id, str(requested_pool))

    if requested:
        if source_ids:
            unknown = sorted(set(requested) - set(source_ids))
            if unknown:
                raise SylveError(
                    "Unknown template storage IDs: " + ", ".join(str(i) for i in unknown)
                )
        elif not requested_pool:
            raise SylveError("Template has no cloneable storage metadata.")

    return [
        {"sourceStorageId": source, "pool": target_pool}
        for source, target_pool in requested.items()
    ]


# ---------------------------------------------------------------------------
# Universal OpenAPI tools
# ---------------------------------------------------------------------------
def _substitute_path(template: str, path_params: dict[str, Any]) -> str:
    placeholders = re.findall(r"\{([^}]+)\}", template)
    missing = [x for x in placeholders if x not in path_params or path_params[x] is None]
    if missing:
        raise SylveError(f"Missing path parameter(s): {', '.join(missing)}")

    def _repl(match: re.Match[str]) -> str:
        return str(path_params[match.group(1)])

    return re.sub(r"\{([^}]+)\}", _repl, template)


def _api_call(params: dict[str, Any], settings: MCPSettings, spec: OpenAPI) -> Any:
    method, template, _op = spec.plan(params)
    node = _g(params, "node")
    with _client_for(params, settings) as client:
        path = _substitute_path(template, _g(params, "path_params") or {})
        query = _g(params, "query_params")
        upload_file = _g(params, "upload_file")
        download_path = _g(params, "download_path")

        if download_path:
            target = client.download_file(path, Path(download_path), params=query, node=node)
            return {"path": str(target), "note": "file streamed to this host path"}

        if upload_file:
            if method.upper() != "POST":
                raise SylveError("upload_file requires POST.")
            source = Path(upload_file).expanduser()
            with source.open("rb") as handle:
                resp = client.post(
                    path,
                    params=query,
                    files={"filepond": (source.name, handle, "application/octet-stream")},
                    node=node,
                )
            return _maybe_wait(client, resp, params)

        resp = client.request(
            method,
            path,
            params=query,
            json_body=_g(params, "body"),
            node=node,
        )
        return _maybe_wait(client, resp, params)


def _api_call_from_descriptor(
    desc: dict[str, Any],
    params: dict[str, Any],
    settings: MCPSettings,
    spec: OpenAPI,
) -> Any:
    op = spec._require_op(desc["path"], desc["method"])  # noqa: SLF001
    where = spec.parameters(op)
    path_params = {p["name"]: params[p["name"]] for p in where["path"] if p["name"] in params}
    query_params = {
        p["name"]: params[p["name"]]
        for p in where["query"]
        if p["name"] in params and p["name"] not in path_params
    }
    built: dict[str, Any] = {
        "method": desc["method"],
        "path": desc["path"],
        "path_params": path_params,
        "query_params": query_params,
        "node": params.get("node"),
        "context": params.get("context"),
        "wait": params.get("wait", True),
        "wait_timeout": params.get("wait_timeout", 60.0),
    }
    if "body" in params:
        built["body"] = params["body"]
    return _api_call(built, settings, spec)


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------
def _ann(
    *,
    title: str | None = None,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
) -> dict[str, Any]:
    annotations: dict[str, Any] = {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": False,
    }
    if title:
        annotations["title"] = title
    return annotations


def _schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = dict(properties)
    props.setdefault(
        "node",
        {
            "type": "string",
            "description": "Target cluster node hostname or UUID. Omit for cluster-wide.",
        },
    )
    props.setdefault(
        "context",
        {"type": "string", "description": "Named login context from ~/.config/sylve/config.yaml."},
    )
    return {"type": "object", "properties": props, "required": required or []}


def _bind(handler: Handler, settings: MCPSettings) -> Callable[[dict[str, Any]], Any]:
    """Bind a server-level settings object into a two-argument handler."""

    def _call(params: dict[str, Any]) -> Any:
        return handler(params, settings)

    return _call


def _register(
    server: MCPServer,
    settings: MCPSettings,
    name: str,
    description: str,
    schema: dict[str, Any],
    handler: Handler,
    annotations: dict[str, Any] | None = None,
) -> None:
    server.register_tool(
        name,
        description,
        schema,
        _bind(handler, settings),
        annotations=annotations,
    )


def _fanned_rows(data: Any, host: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("node", host)
                rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Session & context
# ---------------------------------------------------------------------------
def _whoami(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    cfg = load_config(settings.config_path)
    ctx_name, ctx = cfg.get_active_context(_g(params, "context") or settings.context)
    payload = decode_jwt_payload(ctx.token)
    custom: dict[str, Any] = (
        payload["custom_claims"] if isinstance(payload.get("custom_claims"), dict) else {}
    )
    return {
        "context": ctx_name,
        "server": ctx.server,
        "username": ctx.username or custom.get("username") or "",
        "user_id": custom.get("user_id") or custom.get("userId") or "",
        "auth_type": ctx.auth_type or custom.get("auth_type") or "",
        "hostname": ctx.hostname,
        "node_id": ctx.node_id,
        "default_node": ctx.default_node or "(all/origin)",
        "verify_ssl": ctx.verify_ssl,
        "expires_at": ctx.expires_at,
        "token_status": (
            "EXPIRED" if ctx.is_token_expired() else f"VALID ({ctx.format_remaining()} remaining)"
        ),
    }


def _context_list(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    cfg = load_config(settings.config_path)
    rows = []
    for name, c in cfg.contexts.items():
        rows.append(
            {
                "current": name == cfg.current_context,
                "name": name,
                "server": c.server,
                "username": c.username,
                "auth_type": c.auth_type,
                "hostname": c.hostname,
                "default_node": c.default_node or "",
                "token_status": "EXPIRED" if c.is_token_expired() else "VALID",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Cluster & nodes
# ---------------------------------------------------------------------------
def _cluster_status(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    with _client_for(params, settings) as client:
        details = client.get("/cluster") or {}
        nodes = client.get_cluster_nodes()
        cfg = load_config(settings.config_path)
        ctx_name, _ = cfg.get_active_context(_g(params, "context") or settings.context)
    cluster_cfg = details["cluster"] if isinstance(details.get("cluster"), dict) else {}
    raft_nodes = details["nodes"] if isinstance(details.get("nodes"), list) else []
    online = sum(1 for n in nodes if str(n.get("status") or "").lower() == "online")
    return {
        "context": ctx_name,
        "enabled": bool(cluster_cfg.get("enabled", False)),
        "node_id": details.get("nodeId") or "",
        "leader_id": details.get("leaderId") or "",
        "leader_address": details.get("leaderAddress") or "",
        "partial": bool(details.get("partial", False)),
        "nodes_online": f"{online}/{len(nodes)}",
        "raft_members": raft_nodes,
        "nodes": nodes,
    }


def _cluster_resources(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    with _client_for(params, settings) as client:
        return client.get_cluster_resources()


def _node_list(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    with _client_for(params, settings) as client:
        return client.get_cluster_nodes(refresh=True)


def _node_show(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    node = _g(params, "node")
    with _client_for(params, settings) as client:
        target_host = client.resolve_node_hostname(node)
        combined: dict[str, Any] = {}
        for path in ("/info/basic", "/info/node"):
            try:
                data = client.get(path, node=target_host)
                if isinstance(data, dict):
                    combined.update(data)
            except SylveError:
                pass
        for key, path in (
            ("cpu_details", "/info/cpu"),
            ("ram_details", "/info/ram"),
            ("swap_details", "/info/swap"),
        ):
            try:
                data = client.get(path, node=target_host)
                if isinstance(data, dict) and data:
                    combined[key] = data
            except SylveError:
                pass
        return combined


def _node_audit_logs(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    with _client_for(params, settings) as client:
        results = client.fan_out_get("/info/audit-records", node=_g(params, "node"))
    rows: list[dict[str, Any]] = []
    for host, data in results:
        for row in _fanned_rows(data, host):
            action = row.get("action")
            row["requestPath"] = _audit_path(action)
            rows.append(row)
    return rows


def _audit_path(action: Any) -> str:
    if not isinstance(action, str):
        return "-"
    try:
        return str(json.loads(action).get("path") or "-")
    except (ValueError, TypeError):
        return "-"


def _node_drain(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    """Build and optionally execute a maintenance drain plan (dry run by default)."""
    source_node = str(_g(params, "node") or "").strip()
    if not source_node:
        raise SylveError("A source 'node' (hostname or UUID) is required.")
    target_node = _g(params, "target_node")
    stop_only = _bool(params, "stop_only", False)
    fallback_stop = _bool(params, "fallback_stop", False)
    running_only = _bool(params, "running_only", False)
    dry_run = _bool(params, "dry_run", True)
    ignore_errors = _bool(params, "ignore_errors", False)
    wait = _bool(params, "wait", True)

    with _client_for(params, settings) as client:
        plan = _build_drain_plan(
            client,
            source_node,
            target_node=target_node,
            stop_only=stop_only,
            fallback_stop=fallback_stop,
            include_stopped=not running_only,
        )
        source_uuid, source_host, items, pinned_jobs = plan
        if dry_run:
            return {
                "source_node": source_host,
                "source_uuid": source_uuid,
                "dry_run": True,
                "items": items,
                "pinned_backup_jobs": pinned_jobs,
            }
        if not items:
            return {"source_node": source_host, "items": [], "executed": True}
        blocked = [i for i in items if i["action"] == "blocked"]
        if blocked and not ignore_errors:
            raise SylveError(
                f"{len(blocked)} guest(s) failed migration preflight on '{source_host}'. "
                "Pass fallback_stop=true or ignore_errors=true."
            )
        for item in items:
            if item["action"] in ("skip", "blocked"):
                continue
            try:
                _execute_drain_item(client, item, wait=wait and item.get("taskId") is not None)
            except SylveError as exc:
                item["result"] = f"failed: {exc}"
                if not ignore_errors:
                    raise
        failures = [i for i in items if i["result"].startswith("failed")]
        return {
            "source_node": source_host,
            "source_uuid": source_uuid,
            "executed": True,
            "items": items,
            "pinned_backup_jobs": pinned_jobs,
            "failures": len(failures),
        }


def _build_drain_plan(
    client: SylveClient,
    source_node: str,
    target_node: str | None = None,
    stop_only: bool = False,
    fallback_stop: bool = False,
    include_stopped: bool = True,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    source_uuid, source_host = client.resolve_node_uuid(source_node)
    all_nodes = client.get_cluster_nodes(refresh=True)
    candidate_nodes = [
        n
        for n in all_nodes
        if str(n.get("hostname") or "").lower() != source_host.lower()
        and str(n.get("status") or "online").lower() == "online"
    ]
    explicit_target_uuid = ""
    explicit_target_host = ""
    if target_node and not stop_only:
        explicit_target_uuid, explicit_target_host = client.resolve_node_uuid(target_node)
        if explicit_target_host.lower() == source_host.lower():
            raise SylveError("Target node for drain cannot be the same as the source node.")
    node_uuid_to_host = {
        str(n.get("nodeUUID") or n.get("nodeUuid") or ""): str(n.get("hostname") or "")
        for n in all_nodes
    }

    source_vms: list[dict[str, Any]] = []
    source_jails: list[dict[str, Any]] = []
    for entry in client.get_cluster_resources(refresh=True):
        if str(entry.get("hostname") or "").lower() == source_host.lower():
            source_vms = [v for v in (entry.get("vms") or []) if isinstance(v, dict)]
            source_jails = [j for j in (entry.get("jails") or []) if isinstance(j, dict)]
            break

    policies_by_guest: dict[tuple[str, int], dict[str, Any]] = {}
    try:
        raw = client.get("/cluster/replication/policies") or []
        if isinstance(raw, list):
            for policy in raw:
                if not isinstance(policy, dict) or not policy.get("enabled", True):
                    continue
                active_node = str(policy.get("activeNodeId") or policy.get("sourceNodeId") or "")
                gtype = str(policy.get("guestType") or "").lower()
                gid = int(policy.get("guestId") or 0)
                if active_node.lower() == source_uuid.lower() and gtype and gid > 0:
                    policies_by_guest[(gtype, gid)] = policy
    except SylveError:
        pass

    pinned_backup_jobs: list[dict[str, Any]] = []
    try:
        raw_jobs = client.get("/cluster/backups/jobs") or []
        if isinstance(raw_jobs, list):
            pinned_backup_jobs = [
                job
                for job in raw_jobs
                if isinstance(job, dict)
                and str(job.get("runnerNodeId") or "").lower() == source_uuid.lower()
            ]
    except SylveError:
        pass

    items: list[dict[str, Any]] = []
    guests: list[tuple[str, int, str, str]] = []
    for vm in source_vms:
        rid = int(vm.get("rid") or 0)
        guests.append(
            ("vm", rid, str(vm.get("name") or f"vm-{rid}"), _format_vm_state(vm.get("state")))
        )
    for jail in source_jails:
        ctid = int(jail.get("ctId") or 0)
        guests.append(
            (
                "jail",
                ctid,
                str(jail.get("name") or f"jail-{ctid}"),
                str(jail.get("state") or "UNKNOWN").upper(),
            )
        )

    def _least_loaded(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda n: (
                float(n.get("memoryUsage") or 0.0) * 0.7 + float(n.get("cpuUsage") or 0.0) * 0.3,
                len(n.get("guestIDs") or []),
            ),
        )

    for gtype, gid, name, state_str in guests:
        is_running = state_str in ("RUNNING", "ACTIVE", "PAUSED", "BLOCKED")
        item: dict[str, Any] = {
            "guest_type": gtype,
            "guest_id": gid,
            "name": name,
            "current_state": state_str,
            "source_node": source_host,
        }
        if stop_only:
            item.update(
                {
                    "action": ("shutdown" if gtype == "vm" else "stop") if is_running else "skip",
                    "target_node": "-",
                    "result": "pending" if is_running else "skipped",
                }
            )
            items.append(item)
            continue
        if not include_stopped and not is_running:
            item.update({"action": "skip", "target_node": "-", "result": "skipped"})
            items.append(item)
            continue

        policy = policies_by_guest.get((gtype, gid))
        if policy is not None:
            policy_id = int(policy.get("id") or 0)
            targets = [t for t in (policy.get("targets") or []) if isinstance(t, dict)]
            chosen = explicit_target_uuid
            if not chosen:
                for t in targets:
                    if str(t.get("nodeId") or "").lower() == explicit_target_uuid.lower():
                        chosen = str(t.get("nodeId"))
                        break
            if not chosen and targets:
                ready = [t for t in targets if t.get("ready")]
                pool = ready if ready else targets
                pool.sort(key=lambda t: int(t.get("weight") or 0), reverse=True)
                chosen = str(pool[0].get("nodeId") or "")
            if chosen:
                item.update(
                    {
                        "action": "ha-failover",
                        "target_node": node_uuid_to_host.get(chosen, chosen),
                        "target_node_uuid": chosen,
                        "policy_id": policy_id,
                        "reason": f"replication policy #{policy_id}",
                    }
                )
            else:
                item.update(
                    {
                        "action": "blocked",
                        "target_node": "-",
                        "reason": f"policy #{policy_id} has no matching failover target",
                    }
                )
            items.append(item)
            continue

        dest_uuid = explicit_target_uuid
        dest_host = explicit_target_host
        if not dest_uuid:
            best = _least_loaded(candidate_nodes)
            if best:
                dest_uuid = str(best.get("nodeUUID") or best.get("nodeUuid") or "")
                dest_host = str(best.get("hostname") or "")
        if not dest_uuid:
            if fallback_stop:
                item.update(
                    {
                        "action": ("shutdown" if gtype == "vm" else "stop")
                        if is_running
                        else "skip",
                        "target_node": "-",
                        "result": "pending" if is_running else "skipped",
                        "reason": "no online target node; fallback to stop",
                    }
                )
            else:
                item.update(
                    {
                        "action": "blocked",
                        "target_node": "-",
                        "reason": "no online target node available in cluster",
                    }
                )
            items.append(item)
            continue

        validation_ok = True
        validation_reason = "preflight ok"
        try:
            val_res = client.get(
                "/tasks/migration/validate",
                params={"guestType": gtype, "guestId": gid, "targetNodeUuid": dest_uuid},
                node=source_host,
            )
            if isinstance(val_res, dict) and (
                val_res.get("valid") is False or val_res.get("canMigrate") is False
            ):
                validation_ok = False
                validation_reason = str(
                    val_res.get("reason")
                    or val_res.get("error")
                    or val_res.get("message")
                    or "migration validation failed"
                )
        except SylveError as exc:
            validation_ok = False
            validation_reason = str(exc)
        if validation_ok:
            item.update(
                {
                    "action": "migrate",
                    "target_node": dest_host,
                    "target_node_uuid": dest_uuid,
                    "reason": validation_reason,
                }
            )
        elif fallback_stop:
            item.update(
                {
                    "action": ("shutdown" if gtype == "vm" else "stop") if is_running else "skip",
                    "target_node": "-",
                    "result": "pending" if is_running else "skipped",
                    "reason": f"fallback stop ({validation_reason})",
                }
            )
        else:
            item.update(
                {
                    "action": "blocked",
                    "target_node": dest_host,
                    "reason": validation_reason,
                }
            )
        items.append(item)

    return source_uuid, source_host, items, pinned_backup_jobs


def _execute_drain_item(
    client: SylveClient,
    item: dict[str, Any],
    wait: bool,
) -> None:
    gtype = item["guest_type"]
    gid = item["guest_id"]
    if item["action"] == "ha-failover" and item.get("policy_id"):
        client.post(
            f"/cluster/replication/policies/{item['policy_id']}/failover",
            json_body={
                "targetNodeId": item["target_node_uuid"],
                "mode": "safe",
                "movePinnedSource": True,
                "confirmDataLoss": False,
            },
        )
        item["result"] = "failover-queued"
    elif item["action"] == "migrate":
        endpoint = f"/{gtype}/{gid}/migrations"
        resp = client.post(endpoint, json_body={"targetNodeUuid": item["target_node_uuid"]})
        task_id = resp.get("taskId") if isinstance(resp, dict) else None
        if wait and task_id:
            wait_for_lifecycle_task(client, int(task_id))
            item["result"] = "migrated"
        else:
            item["result"] = f"queued (task #{task_id})" if task_id else "queued"
    elif item["action"] in ("shutdown", "stop"):
        resp = client.post(f"/{gtype}/{gid}/actions/{item['action']}")
        task_id = resp.get("taskId") if isinstance(resp, dict) else None
        if wait and task_id:
            wait_for_lifecycle_task(client, int(task_id))
            item["result"] = "stopped"
        else:
            item["result"] = f"queued (task #{task_id})" if task_id else "stopped"


# ---------------------------------------------------------------------------
# VMs
# ---------------------------------------------------------------------------
def _vm_list(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    detailed = _bool(params, "detailed", False)
    with _client_for(params, settings) as client:
        rows: list[dict[str, Any]] = []
        target_host = client.resolve_node_hostname(_g(params, "node"))
        if detailed:
            for host, data in client.fan_out_get("/vm", node=target_host):
                if isinstance(data, list):
                    for vm in data:
                        if not isinstance(vm, dict):
                            continue
                        rows.append(
                            {
                                "rid": vm.get("rid"),
                                "name": vm.get("name"),
                                "state": _format_vm_state(vm.get("state")),
                                "node": host,
                                "vcpus": int(vm.get("cpuSockets") or 1)
                                * int(vm.get("cpuCores") or 1)
                                * int(vm.get("cpuThreads") or 1),
                                "ram": vm.get("ram"),
                                "vnc_port": vm.get("vncPort") if vm.get("vncEnabled") else None,
                                "autostart": bool(vm.get("startAtBoot")),
                                "description": vm.get("description") or "",
                            }
                        )
        else:
            for node_res in client.get_cluster_resources():
                host = str(node_res.get("hostname") or "")
                if target_host and host.lower() != target_host.lower():
                    continue
                for vm in node_res.get("vms") or []:
                    if not isinstance(vm, dict):
                        continue
                    rows.append(
                        {
                            "rid": vm.get("rid"),
                            "name": vm.get("name"),
                            "state": _format_vm_state(vm.get("state")),
                            "node": host,
                            "vnc_port": vm.get("vncPort"),
                        }
                    )
        return rows


def _vm_show(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    ident = str(_g(params, "vm") or "")
    if not ident:
        raise SylveError("'vm' is required (RID or name).")
    with _client_for(params, settings) as client:
        rid, host, summary = client.resolve_vm(ident, node=_g(params, "node"))
        data = client.get(f"/vm/{rid}", node=host or None)
        if not isinstance(data, dict):
            raise SylveError(f"Unexpected response for VM {rid}.")
        if "state" in summary:
            data["state"] = summary["state"]
        data["state_name"] = _format_vm_state(data.get("state"))
        data["node"] = host or client.context.hostname or "local"
        if data.get("qemuGuestAgent") and data["state_name"] == "RUNNING":
            try:
                agent = client.get(f"/vm/{rid}/guest-agent", node=host or None)
                data["ipAddresses"] = _guest_ip_addresses(agent)
            except SylveError:
                pass
        return data


def _guest_ip_addresses(agent: Any) -> list[str]:
    if not isinstance(agent, dict):
        return []
    addresses: list[str] = []
    for iface in agent.get("interfaces") or []:
        if not isinstance(iface, dict):
            continue
        name = str(iface.get("name") or "")
        if not name or name == "lo":
            continue
        for entry in iface.get("ip-addresses") or []:
            if not isinstance(entry, dict):
                continue
            address = str(entry.get("ip-address") or "")
            if not address or address == "::1" or address.startswith(("127.", "fe80:")):
                continue
            addresses.append(f"{name}: {address}")
    return addresses


def _vm_create(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        host = _resolve_host(client, node)
        dry_run = _bool(params, "dry_run", False)
        start = _bool(params, "start", False)
        wait = _bool(params, "wait", start)
        template_ref = _g(params, "template")

        if template_ref is not None:
            return _vm_create_from_template(
                client, host, params, template_ref, dry_run, start, wait
            )

        payload = _vm_scratch_payload(params)
        payload = _normalize_scratch_payload(client, host, payload)
        result: dict[str, Any] = {
            "mode": "scratch",
            "node": host,
            "payload": payload,
            "dry_run": dry_run,
        }
        if dry_run:
            return result
        response = _post_scratch(client, host, payload)
        result.update(_maybe_wait(client, response, params))
        rid = (response or {}).get("rid") if isinstance(response, dict) else None
        result["rid"] = rid or payload.get("rid")
        result["name"] = payload.get("name")
        if start:
            task = _start_vm(client, int(result["rid"]), host, wait=True)
            result["started"] = True
            result["start_response"] = task
        return result


def _vm_create_from_template(
    client: SylveClient,
    host: str,
    params: dict[str, Any],
    template_ref: Any,
    dry_run: bool,
    start: bool,
    wait: bool,
) -> dict[str, Any]:
    template_id, template_label = _resolve_template(client, host, str(template_ref))
    name = _g(params, "name")
    rid = _g(params, "rid")
    start_rid = _g(params, "start_rid")
    count = _g(params, "count")
    name_prefix = _g(params, "name_prefix")
    pool = _g(params, "pool")

    multiple = bool(start_rid is not None or name_prefix is not None or (count or 1) > 1)
    request: dict[str, Any] = {"mode": "multiple" if multiple else "single"}
    if multiple:
        start_value = start_rid
        if start_value in (None, "", 0, "0"):
            start_value = _next_guest_id(client)
        start_value = _as_int(start_value, "startRid")
        vm_count = _as_int(count or 1, "count")
        if not 1 <= vm_count <= 200:
            raise SylveError("count must be between 1 and 200.")
        if start_value < 1 or start_value + vm_count - 1 > 9999:
            raise SylveError("Template RID range must stay within 1-9999.")
        request["startRid"] = start_value
        request["count"] = vm_count
        if name_prefix:
            request["namePrefix"] = str(name_prefix)
    else:
        rid_value = rid
        if rid_value in (None, "", 0, "0"):
            rid_value = _next_guest_id(client)
        rid_value = _as_int(rid_value, "rid")
        if not 1 <= rid_value <= 9999:
            raise SylveError("RID must be between 1 and 9999.")
        request["rid"] = rid_value
        if name:
            request["name"] = str(name)

    storage_pools_input = _g(params, "storage_pools")
    if pool is not None or storage_pools_input:
        detail: dict[str, Any] = {}
        try:
            detail = client.get(f"/vm/templates/{template_id}", node=host) or {}
        except SylveError:
            pass
        detail = detail if isinstance(detail, dict) else {}
        request["storagePools"] = _template_storage_mappings(
            client, host, template_id, detail, pool, storage_pools_input
        )

    request["rewriteCloudInitIdentity"] = _bool(params, "rewrite_cloud_init_identity", False)
    if request["rewriteCloudInitIdentity"] and _g(params, "cloud_init_prefix"):
        request["cloudInitPrefix"] = str(_g(params, "cloud_init_prefix"))

    result: dict[str, Any] = {
        "mode": "template",
        "templateId": template_id,
        "template": template_label,
        "node": host,
        "request": request,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    response = _post_template(client, host, template_id, request)
    result.update(_maybe_wait(client, response, params))
    task_id = (response or {}).get("taskId") if isinstance(response, dict) else None
    if task_id:
        result["taskId"] = int(task_id)
    if start:
        rids = (
            [request["rid"]]
            if not multiple
            else list(range(request["startRid"], request["startRid"] + request["count"]))
        )
        started = []
        for vm_rid in rids:
            started.append(_start_vm(client, vm_rid, host, wait=True))
        result["started"] = True
        result["started_rids"] = rids
    return result


def _vm_scratch_payload(params: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    config = _g(params, "config")
    if isinstance(config, dict):
        for source, target in _SCRATCH_ALIASES.items():
            if source in config and config[source] is not None and target not in payload:
                payload[target] = config[source]
        for key in ("name", "rid", "description"):
            if config.get(key) is not None:
                payload[key] = config[key]

    for source, target in _SCRATCH_ALIASES.items():
        value = _g(params, source)
        if value is not None:
            payload[target] = value
    for key in ("name", "rid", "description"):
        if _g(params, key) is not None:
            payload[key] = _g(params, key)

    if _g(params, "vnc") is not None:
        payload["vncEnabled"] = _bool(params, "vnc", True)
    if _g(params, "cloud_init") is not None:
        payload["cloudInit"] = _bool(params, "cloud_init", False)
    if _g(params, "user_data") is not None:
        payload["cloudInitData"] = _g(params, "user_data")
    if _g(params, "meta_data") is not None:
        payload["cloudInitMetaData"] = _g(params, "meta_data")
    if _g(params, "network_config") is not None:
        payload["cloudInitNetworkConfig"] = _g(params, "network_config")
    return _coerce_scratch_values(payload)


def _coerce_scratch_values(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("ram", "storageSize"):
        if key in payload:
            payload[key] = _parse_size(payload[key], key)
    for key in ("rid", "cpuSockets", "cpuCores", "cpuThreads", "vncPort", "startOrder"):
        if key in payload and payload[key] not in (None, ""):
            payload[key] = _as_int(payload[key], key)
    for key in (
        "tpmEmulation",
        "serial",
        "vncEnabled",
        "vncWait",
        "startAtBoot",
        "apic",
        "acpi",
        "cloudInit",
        "ignoreUMSR",
        "qemuGuestAgent",
    ):
        if key in payload:
            payload[key] = _as_bool(payload[key], key)
    if "pciDevices" in payload and isinstance(payload["pciDevices"], str):
        payload["pciDevices"] = [
            item.strip() for item in payload["pciDevices"].split(",") if item.strip()
        ]
    if "extraBhyveOptions" in payload and isinstance(payload["extraBhyveOptions"], str):
        payload["extraBhyveOptions"] = [
            item.strip() for item in payload["extraBhyveOptions"].split() if item.strip()
        ]
    return payload


def _post_scratch(client: SylveClient, host: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.post("/vm", json_body=payload, node=host)
    except SylveAPIError as exc:
        raise SylveError(_friendly_create_error(exc)) from exc
    return response if isinstance(response, dict) else {}


def _post_template(
    client: SylveClient,
    host: str,
    template_id: int,
    request: dict[str, Any],
) -> dict[str, Any]:
    try:
        response = client.post(f"/vm/templates/{template_id}/vms", json_body=request, node=host)
    except SylveAPIError as exc:
        raise SylveError(_friendly_create_error(exc)) from exc
    return response if isinstance(response, dict) else {}


def _start_vm(client: SylveClient, rid: int, host: str, wait: bool) -> dict[str, Any]:
    try:
        response = client.post(f"/vm/{rid}/actions/start", node=host)
    except SylveAPIError as exc:
        raise SylveError(f"VM {rid} was created but could not be started: {exc}") from exc
    task_id = response.get("taskId") if isinstance(response, dict) else None
    if wait and task_id:
        wait_for_lifecycle_task(client, int(task_id), node=host)
    return response if isinstance(response, dict) else {}


def _vm_action(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    action = str(_g(params, "action") or "").lower()
    if action not in ("start", "stop", "shutdown", "reboot", "restart"):
        raise SylveError("action must be start, stop, shutdown, reboot, or restart.")
    idents = _g(params, "vms")
    if not isinstance(idents, list) or not idents:
        raise SylveError("'vms' must be a non-empty list of RIDs or names.")
    with _client_for(params, settings) as client:
        results: list[dict[str, Any]] = []
        for ident in idents:
            rid, host, _ = client.resolve_vm(ident, node=_g(params, "node"))
            resp = client.post(f"/vm/{rid}/actions/{action}", node=host or None)
            entry: dict[str, Any] = {"rid": rid, "node": host or "local", "action": action}
            entry.update(_dict_or_empty(_maybe_wait(client, resp, params)))
            results.append(entry)
        return results


def _vm_migrate(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    vm = str(_g(params, "vm") or "")
    target = str(_g(params, "target") or "")
    if not vm or not target:
        raise SylveError("'vm' and 'target' are required.")
    validate_only = _bool(params, "validate_only", False)
    with _client_for(params, settings) as client:
        rid, source_host, _ = client.resolve_vm(vm, node=_g(params, "node"))
        target_uuid, target_host = client.resolve_node_uuid(target)
        preflight = client.get(
            "/tasks/migration/validate",
            params={"guestType": "vm", "guestId": rid, "targetNodeUuid": target_uuid},
            node=source_host or None,
        )
        if validate_only:
            return {
                "rid": rid,
                "source_node": source_host,
                "target_node": target_host,
                "preflight": preflight or {"valid": True},
            }
        resp = client.post(
            f"/vm/{rid}/migrations",
            json_body={"targetNodeUuid": target_uuid},
            node=source_host or None,
        )
        return {"rid": rid, "source_node": source_host, "target": target_host} | dict(
            _maybe_wait(client, resp, params)
        )


def _vm_delete(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    vm = str(_g(params, "vm") or "")
    if not vm:
        raise SylveError("'vm' is required.")
    with _client_for(params, settings) as client:
        rid, host, _ = client.resolve_vm(vm, node=_g(params, "node"))
        client.delete(
            f"/vm/{rid}",
            params={
                "deletevolumes": _bool(params, "delete_volumes", True),
                "deleterawdisks": _bool(params, "delete_raw_disks", True),
                "deletemacs": _bool(params, "delete_macs", True),
                "force": _bool(params, "force", False),
            },
            node=host or None,
        )
        return {"deleted": True, "rid": rid, "node": host or "local"}


def _vm_logs(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    vm = str(_g(params, "vm") or "")
    if not vm:
        raise SylveError("'vm' is required.")
    with _client_for(params, settings) as client:
        rid, host, _ = client.resolve_vm(vm, node=_g(params, "node"))
        data = client.get(f"/vm/{rid}/logs", node=host or None)
        if isinstance(data, dict):
            return {"rid": rid, "logs": str(data.get("logs") or "")}
        return {"rid": rid, "logs": str(data or "")}


def _vm_snapshot(params: dict[str, Any], settings: MCPSettings) -> Any:
    return _guest_snapshots(
        params, settings, kind="vm", ident_key="vm", list_path="/vm/{id}/snapshots"
    )


def _jail_snapshot(params: dict[str, Any], settings: MCPSettings) -> Any:
    return _guest_snapshots(
        params, settings, kind="jail", ident_key="jail", list_path="/jail/{id}/snapshots"
    )


def _guest_snapshots(
    params: dict[str, Any],
    settings: MCPSettings,
    kind: str,
    ident_key: str,
    list_path: str,
) -> Any:
    action = str(_g(params, "action") or "list").lower()
    ident = str(_g(params, ident_key) or "")
    if not ident:
        raise SylveError(f"'{ident_key}' is required.")
    with _client_for(params, settings) as client:
        if kind == "vm":
            gid, host, _ = client.resolve_vm(ident, node=_g(params, "node"))
        else:
            gid, host, _ = client.resolve_jail(ident, node=_g(params, "node"))
        base = list_path.replace("{id}", str(gid))
        if action == "list":
            data = client.get(base, node=host or None) or []
            return data if isinstance(data, list) else []
        if action == "create":
            resp = client.post(
                base,
                json_body={
                    "name": str(_g(params, "name") or ""),
                    "description": str(_g(params, "description") or ""),
                },
                node=host or None,
            )
            return {"created": True, "id": gid} | _dict_or_empty(_maybe_wait(client, resp, params))
        snapshot_id = _g(params, "snapshot_id")
        if snapshot_id is None:
            raise SylveError("'snapshot_id' is required for rollback/delete.")
        if action == "rollback":
            resp = client.post(f"{base}/{snapshot_id}/rollback", node=host or None)
            return {"rolled_back": True, "id": gid, "snapshot_id": int(snapshot_id)}
        if action == "delete":
            client.delete(f"{base}/{snapshot_id}", node=host or None)
            return {"deleted": True, "id": gid, "snapshot_id": int(snapshot_id)}
        raise SylveError("action must be list, create, rollback, or delete.")


def _vm_template(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "list").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "list":
            rows = []
            for host, data in client.fan_out_get("/vm/templates", node=node):
                for row in _fanned_rows(data, host):
                    client.enrich_template_metadata(row, f"/vm/templates/{row.get('id')}", host)
                    rows.append(row)
            return rows
        if action == "show":
            template_id = _g(params, "template_id")
            if template_id is None:
                raise SylveError("'template_id' is required.")
            return client.get(f"/vm/templates/{template_id}", node=node)
        if action == "capture":
            vm = str(_g(params, "vm") or "")
            name = str(_g(params, "name") or "")
            if not vm or not name:
                raise SylveError("'vm' and 'name' are required for capture.")
            rid, host, _ = client.resolve_vm(vm, node=node)
            resp = client.post(f"/vm/{rid}/templates", json_body={"name": name}, node=host or None)
            return {"captured": True, "rid": rid, "name": name} | dict(
                _maybe_wait(client, resp, params)
            )
        if action == "deploy":
            template_id = _g(params, "template_id")
            if template_id is None:
                raise SylveError("'template_id' is required for deploy.")
            body: dict[str, Any] = {"count": _as_int(_g(params, "count", 1), "count")}
            if _g(params, "name"):
                body["name"] = str(_g(params, "name"))
            resp = client.post(f"/vm/templates/{template_id}/vms", json_body=body, node=node)
            return {"deployed": True, "template_id": int(template_id)} | dict(
                _maybe_wait(client, resp, params)
            )
        if action == "delete":
            template_id = _g(params, "template_id")
            if template_id is None:
                raise SylveError("'template_id' is required.")
            client.delete(f"/vm/templates/{template_id}", node=node)
            return {"deleted": True, "template_id": int(template_id)}
        raise SylveError("action must be list, show, capture, deploy, or delete.")


# ---------------------------------------------------------------------------
# Jails
# ---------------------------------------------------------------------------
def _jail_list(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    with _client_for(params, settings) as client:
        rows: list[dict[str, Any]] = []
        target_host = client.resolve_node_hostname(_g(params, "node"))
        for node_res in client.get_cluster_resources():
            host = str(node_res.get("hostname") or "")
            if target_host and host.lower() != target_host.lower():
                continue
            for jail in node_res.get("jails") or []:
                if not isinstance(jail, dict):
                    continue
                rows.append(
                    {
                        "ctId": jail.get("ctId"),
                        "name": jail.get("name"),
                        "state": str(jail.get("state") or "UNKNOWN").upper(),
                        "type": jail.get("type") or "freebsd",
                        "node": host,
                        "cores": jail.get("cores"),
                        "memory": jail.get("memory"),
                    }
                )
        return rows


def _jail_show(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    ident = str(_g(params, "jail") or "")
    if not ident:
        raise SylveError("'jail' is required (CTID or name).")
    with _client_for(params, settings) as client:
        ctid, host, summary = client.resolve_jail(ident, node=_g(params, "node"))
        data = client.get(f"/jail/{ctid}", node=host or None)
        if not isinstance(data, dict):
            raise SylveError(f"Unexpected response showing Jail {ctid}.")
        data["state"] = summary.get("state") or data.get("state") or "UNKNOWN"
        data["node"] = host or client.context.hostname or "local"
        try:
            guest = client.get(f"/jail/{ctid}/guest-info", node=host or None)
            if isinstance(guest, dict):
                data["guest_info"] = guest
        except SylveError:
            pass
        return data


def _jail_action(params: dict[str, Any], settings: MCPSettings) -> list[dict[str, Any]]:
    action = str(_g(params, "action") or "").lower()
    if action not in ("start", "stop", "restart"):
        raise SylveError("action must be start, stop, or restart.")
    idents = _g(params, "jails")
    if not isinstance(idents, list) or not idents:
        raise SylveError("'jails' must be a non-empty list of CTIDs or names.")
    with _client_for(params, settings) as client:
        results: list[dict[str, Any]] = []
        for ident in idents:
            ctid, host, _ = client.resolve_jail(ident, node=_g(params, "node"))
            resp = client.post(f"/jail/{ctid}/actions/{action}", node=host or None)
            entry: dict[str, Any] = {"ctId": ctid, "node": host or "local", "action": action}
            entry.update(_dict_or_empty(_maybe_wait(client, resp, params)))
            results.append(entry)
        return results


def _jail_create(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    name = str(_g(params, "name") or "").strip()
    pool = str(_g(params, "pool") or "").strip()
    if not name or not pool:
        raise SylveError("'name' and 'pool' are required.")
    kind = str(_g(params, "type") or "freebsd").strip().lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        host = _resolve_host(client, node)
        payload: dict[str, Any] = {
            "name": name,
            "pool": pool,
            "type": kind,
        }
        ctid = _g(params, "ctid")
        if ctid in (None, "", 0, "0"):
            ctid = _next_guest_id(client)
        payload["ctId"] = _as_int(ctid, "ctId")
        for source, target in (
            ("base", "base"),
            ("bootstrap_name", "bootstrapName"),
            ("hostname", "hostname"),
            ("description", "description"),
            ("switch_name", "switchName"),
            ("vlan_policy", "vlanPolicy"),
            ("devfs_ruleset", "devfsRuleset"),
            ("fstab", "fstab"),
            ("resolv_conf", "resolvConf"),
            ("ipv4_raw", "ipv4Raw"),
            ("ipv4_gw_raw", "ipv4GwRaw"),
            ("ipv6_raw", "ipv6Raw"),
            ("ipv6_gw_raw", "ipv6GwRaw"),
            ("mac_raw", "macRaw"),
            ("metadata_meta", "metadataMeta"),
            ("metadata_env", "metadataEnv"),
            ("additional_options", "additionalOptions"),
        ):
            if _g(params, source) is not None:
                payload[target] = _g(params, source)
        if _g(params, "cores") is not None:
            payload["cores"] = _as_int(_g(params, "cores"), "cores")
        if _g(params, "memory") is not None:
            payload["memory"] = _parse_size(_g(params, "memory"), "memory")
        for key in (
            "dhcp",
            "inherit_ipv4",
            "inherit_ipv6",
            "slaac",
            "clean_environment",
            "resource_limits",
            "start_at_boot",
        ):
            if _g(params, key) is not None:
                payload[key.replace("_", "")] = _as_bool(_g(params, key), key)
        if _g(params, "start_order") is not None:
            payload["startOrder"] = _as_int(_g(params, "start_order"), "startOrder")
        if _g(params, "allowed_options") is not None:
            value = _g(params, "allowed_options")
            payload["allowedOptions"] = value if isinstance(value, list) else [str(value)]
        response = client.post("/jail", json_body=payload, node=host)
        return {"created": True, "name": name, "ctId": payload["ctId"], "node": host} | dict(
            _maybe_wait(client, response, params)
        )


def _jail_migrate(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    jail = str(_g(params, "jail") or "")
    target = str(_g(params, "target") or "")
    if not jail or not target:
        raise SylveError("'jail' and 'target' are required.")
    validate_only = _bool(params, "validate_only", False)
    with _client_for(params, settings) as client:
        ctid, source_host, _ = client.resolve_jail(jail, node=_g(params, "node"))
        target_uuid, target_host = client.resolve_node_uuid(target)
        preflight = client.get(
            "/tasks/migration/validate",
            params={"guestType": "jail", "guestId": ctid, "targetNodeUuid": target_uuid},
            node=source_host or None,
        )
        if validate_only:
            return {
                "ctId": ctid,
                "source_node": source_host,
                "target_node": target_host,
                "preflight": preflight or {"valid": True},
            }
        resp = client.post(
            f"/jail/{ctid}/migrations",
            json_body={"targetNodeUuid": target_uuid},
            node=source_host or None,
        )
        return {"ctId": ctid, "source_node": source_host, "target": target_host} | dict(
            _maybe_wait(client, resp, params)
        )


def _jail_delete(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    jail = str(_g(params, "jail") or "")
    if not jail:
        raise SylveError("'jail' is required.")
    with _client_for(params, settings) as client:
        ctid, host, _ = client.resolve_jail(jail, node=_g(params, "node"))
        client.delete(
            f"/jail/{ctid}",
            params={
                "deleterootfs": _bool(params, "delete_rootfs", True),
                "deletemacs": _bool(params, "delete_macs", True),
            },
            node=host or None,
        )
        return {"deleted": True, "ctId": ctid, "node": host or "local"}


def _jail_logs(params: dict[str, Any], settings: MCPSettings) -> dict[str, Any]:
    jail = str(_g(params, "jail") or "")
    if not jail:
        raise SylveError("'jail' is required.")
    with _client_for(params, settings) as client:
        ctid, host, _ = client.resolve_jail(jail, node=_g(params, "node"))
        data = client.get(f"/jail/{ctid}/logs", node=host or None)
        if isinstance(data, dict):
            return {"ctId": ctid, "logs": str(data.get("logs") or "")}
        return {"ctId": ctid, "logs": str(data or "")}


def _jail_bootstrap(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "list").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "list":
            rows = []
            for host, data in client.fan_out_get(
                "/jail/bootstraps", params={"pool": _g(params, "pool")}, node=node
            ):
                rows.extend(_fanned_rows(data, host))
            return rows
        if action == "create":
            pool = str(_g(params, "pool") or "")
            major = _g(params, "major")
            if not pool or major is None:
                raise SylveError("'pool' and 'major' are required for create.")
            resp = client.post(
                "/jail/bootstraps",
                json_body={
                    "pool": pool,
                    "major": _as_int(major, "major"),
                    "minor": _as_int(_g(params, "minor", 0), "minor"),
                    "type": _g(params, "type") or "base",
                },
                node=node,
            )
            return {"queued": True, "pool": pool} | _dict_or_empty(
                _maybe_wait(client, resp, params)
            )
        if action == "delete":
            name = str(_g(params, "name") or "")
            if not name:
                raise SylveError("'name' is required for delete.")
            client.delete(f"/jail/bootstraps/{name}", node=node)
            return {"deleted": True, "name": name}
        raise SylveError("action must be list, create, or delete.")


def _jail_template(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "list").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "list":
            rows = []
            for host, data in client.fan_out_get("/jail/templates", node=node):
                for row in _fanned_rows(data, host):
                    client.enrich_template_metadata(row, f"/jail/templates/{row.get('id')}", host)
                    rows.append(row)
            return rows
        if action == "show":
            template_id = _g(params, "template_id")
            if template_id is None:
                raise SylveError("'template_id' is required.")
            return client.get(f"/jail/templates/{template_id}", node=node)
        if action == "capture":
            jail = str(_g(params, "jail") or "")
            name = str(_g(params, "name") or "")
            if not jail or not name:
                raise SylveError("'jail' and 'name' are required for capture.")
            ctid, host, _ = client.resolve_jail(jail, node=node)
            resp = client.post(
                f"/jail/{ctid}/templates", json_body={"name": name}, node=host or None
            )
            return {"captured": True, "ctId": ctid, "name": name} | dict(
                _maybe_wait(client, resp, params)
            )
        if action == "deploy":
            template_id = _g(params, "template_id")
            if template_id is None:
                raise SylveError("'template_id' is required for deploy.")
            body: dict[str, Any] = {"count": _as_int(_g(params, "count", 1), "count")}
            resp = client.post(f"/jail/templates/{template_id}/jails", json_body=body, node=node)
            return {"deployed": True, "template_id": int(template_id)} | dict(
                _maybe_wait(client, resp, params)
            )
        if action == "delete":
            template_id = _g(params, "template_id")
            if template_id is None:
                raise SylveError("'template_id' is required.")
            client.delete(f"/jail/templates/{template_id}", node=node)
            return {"deleted": True, "template_id": int(template_id)}
        raise SylveError("action must be list, show, capture, deploy, or delete.")


# ---------------------------------------------------------------------------
# ZFS
# ---------------------------------------------------------------------------
def _zfs_pool(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "list").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "list":
            rows = []
            for host, data in client.fan_out_get("/zfs/pools", node=node):
                rows.extend(_fanned_rows(data, host))
            return rows
        if action == "disks_usage":
            data = client.get("/zfs/pools/disks-usage", node=node)
            return data if isinstance(data, list) else []
        pool = str(_g(params, "pool") or "")
        if not pool:
            raise SylveError("'pool' (name or GUID) is required.")
        guid, host = client.resolve_pool_guid(pool, node=node)
        if action == "status":
            data = client.get(f"/zfs/pools/{guid}/status", node=host or None) or {}
            data["node"] = host
            return data
        if action == "scrub":
            client.post(f"/zfs/pools/{guid}/scrub", node=host or None)
            return {"scrub_started": True, "pool": pool, "node": host}
        raise SylveError("action must be list, status, scrub, or disks_usage.")


def _zfs_dataset(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "list").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "list":
            dtype = _g(params, "type")
            rows = []
            for host, data in client.fan_out_get(
                "/zfs/datasets",
                params={"type": dtype.upper()} if dtype else None,
                node=node,
            ):
                rows.extend(_fanned_rows(data, host))
            return rows
        if action == "create_filesystem":
            parent = str(_g(params, "parent") or "")
            name = str(_g(params, "name") or "")
            if not parent or not name:
                raise SylveError("'parent' and 'name' are required.")
            resp = client.post(
                "/zfs/datasets/filesystem",
                json_body={
                    "parent": parent,
                    "name": name,
                    "properties": _g(params, "properties") or {},
                },
                node=node,
            )
            return {"created": True, "parent": parent, "name": name} | dict(
                _maybe_wait(client, resp, params)
            )
        if action == "create_volume":
            parent = str(_g(params, "parent") or "")
            name = str(_g(params, "name") or "")
            if not parent or not name:
                raise SylveError("'parent' and 'name' are required.")
            properties = dict(_g(params, "properties") or {})
            if _g(params, "size") is not None:
                size = _parse_size(_g(params, "size"), "size")
                properties["volsize"] = str(size)
            resp = client.post(
                "/zfs/datasets/volume",
                json_body={"parent": parent, "name": name, "properties": properties},
                node=node,
            )
            return {"created": True, "parent": parent, "name": name} | dict(
                _maybe_wait(client, resp, params)
            )
        dataset = str(_g(params, "dataset") or "")
        if not dataset:
            raise SylveError("'dataset' (name or GUID) is required.")
        guid, host = client.resolve_dataset_guid(dataset, node=node)
        if action == "delete":
            norm_type = str(_g(params, "type") or "filesystem").strip().lower()
            if norm_type not in ("filesystem", "volume"):
                raise SylveError("delete 'type' must be 'filesystem' or 'volume'.")
            client.delete(f"/zfs/datasets/{norm_type}/{guid}", node=host or None)
            return {"deleted": True, "dataset": dataset, "node": host}
        if action == "edit":
            norm_type = str(_g(params, "type") or "filesystem").strip().lower()
            if norm_type not in ("filesystem", "volume"):
                raise SylveError("edit 'type' must be 'filesystem' or 'volume'.")
            resp = client.patch(
                f"/zfs/datasets/{norm_type}/{guid}",
                json_body={"properties": _g(params, "properties") or {}},
                node=host or None,
            )
            return {"edited": True, "dataset": dataset, "node": host} | dict(
                _maybe_wait(client, resp, params)
            )
        raise SylveError("action must be list, create_filesystem, create_volume, edit, or delete.")


def _zfs_snapshot(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "list").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "list":
            rows = []
            for host, data in client.fan_out_get(
                "/zfs/datasets", params={"type": "SNAPSHOT"}, node=node
            ):
                rows.extend(_fanned_rows(data, host))
            return rows
        if action == "periodic_list":
            rows = []
            for host, data in client.fan_out_get("/zfs/datasets/snapshot/periodic", node=node):
                rows.extend(_fanned_rows(data, host))
            return rows
        ident = str(_g(params, "snapshot") or _g(params, "dataset") or "")
        if not ident:
            raise SylveError("'snapshot' (dataset@snap or GUID) is required.")
        guid, host = client.resolve_dataset_guid(ident, dtype="SNAPSHOT", node=node)
        if action == "create":
            name = str(_g(params, "name") or "")
            if not name:
                raise SylveError("'name' (after @) is required.")
            resp = client.post(
                "/zfs/datasets/snapshot",
                json_body={
                    "guid": guid,
                    "name": name,
                    "recursive": _bool(params, "recursive", False),
                },
                node=host or None,
            )
            return {"created": True, "snapshot": f"{ident}@{name}", "node": host} | dict(
                _maybe_wait(client, resp, params)
            )
        if action == "rollback":
            resp = client.post(
                f"/zfs/datasets/snapshot/{guid}/rollback",
                json_body={"destroyMoreRecent": _bool(params, "destroy_more_recent", False)},
                node=host or None,
            )
            return {"rolled_back": True, "snapshot": ident, "node": host} | dict(
                _maybe_wait(client, resp, params)
            )
        if action == "delete":
            client.delete(
                f"/zfs/datasets/snapshot/{guid}",
                params={"recursive": _bool(params, "recursive", False)},
                node=host or None,
            )
            return {"deleted": True, "snapshot": ident, "node": host}
        raise SylveError("action must be list, create, rollback, delete, or periodic_list.")


def _download(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "list").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "list":
            rows = []
            for host, data in client.fan_out_get("/utilities/downloads", node=node):
                rows.extend(_fanned_rows(data, host))
            return rows
        if action == "show":
            ident = str(_g(params, "identifier") or "").strip().lower()
            if not ident:
                raise SylveError("'identifier' (ID, UUID, or name) is required.")
            for host, data in client.fan_out_get("/utilities/downloads", node=node):
                if isinstance(data, list):
                    for dl in data:
                        if not isinstance(dl, dict):
                            continue
                        if ident in {
                            str(dl.get("id") or "").lower(),
                            str(dl.get("uuid") or "").lower(),
                            str(dl.get("name") or "").lower(),
                        }:
                            out = dict(dl)
                            out["node"] = host
                            return out
            raise SylveError(f"Download '{ident}' not found.")
        if action == "add":
            url = str(_g(params, "url") or "")
            if not url:
                raise SylveError("'url', 'magnet', or server path is required.")
            payload: dict[str, Any] = {
                "url": url,
                "downloadType": _g(params, "download_type") or "uncategorized",
                "automaticExtraction": _bool(params, "extract", True),
                "automaticRawConversion": _bool(params, "raw_convert", False),
                "ignoreTLS": _bool(params, "ignore_tls", False),
            }
            for source, target in (
                ("filename", "filename"),
                ("storage_pool", "storagePool"),
            ):
                if _g(params, source) is not None:
                    payload[target] = _g(params, source)
            resp = client.post("/utilities/downloads", json_body=payload, node=node)
            return {"queued": True, "url": url} | _dict_or_empty(_maybe_wait(client, resp, params))
        if action == "delete":
            ids = _g(params, "ids")
            if not isinstance(ids, list) or not ids:
                raise SylveError("'ids' must be a non-empty list of download IDs.")
            if len(ids) == 1:
                client.delete(f"/utilities/downloads/{ids[0]}", node=node)
            else:
                client.post(
                    "/utilities/downloads/bulk-delete",
                    json_body={"ids": [_as_int(i, "id") for i in ids]},
                    node=node,
                )
            return {"deleted": True, "ids": ids}
        raise SylveError("action must be list, show, add, or delete.")


# ---------------------------------------------------------------------------
# Network, replication, backups, tasks
# ---------------------------------------------------------------------------
_NETWORK_ENDPOINTS: dict[str, str] = {
    "interfaces": "/network/interface",
    "switches": "/network/switch",
    "routes": "/network/route",
    "objects": "/network/object",
    "firewall_traffic": "/network/firewall/traffic",
    "firewall_nat": "/network/firewall/nat",
    "firewall_advanced": "/network/firewall/advanced",
    "firewall_rendered": "/network/firewall/advanced/rendered",
    "wireguard": "/network/wireguard/status",
    "global_config": "/network/global-config",
}


def _network_overview(params: dict[str, Any], settings: MCPSettings) -> Any:
    kind = str(_g(params, "kind") or "interfaces").strip().lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if kind in _NETWORK_ENDPOINTS:
            return client.get(_NETWORK_ENDPOINTS[kind], node=node)
        if kind == "dhcp":
            return {
                "config": client.get("/network/dhcp/config", node=node),
                "ranges": client.get("/network/dhcp/range", node=node),
                "leases": client.get("/network/dhcp/lease", node=node),
            }
        if kind == "firewall_counters":
            return {
                "traffic": client.get("/network/firewall/traffic/counters", node=node),
                "nat": client.get("/network/firewall/nat/counters", node=node),
            }
        if kind == "all":
            return {name: client.get(path, node=node) for name, path in _NETWORK_ENDPOINTS.items()}
        raise SylveError(
            "kind must be one of: "
            + ", ".join(list(_NETWORK_ENDPOINTS) + ["dhcp", "firewall_counters", "all"])
        )


def _replication(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "policies").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "policies":
            return client.get("/cluster/replication/policies", node=node)
        if action == "policy_show":
            pid = _g(params, "policy_id")
            if pid is None:
                raise SylveError("'policy_id' is required.")
            return client.get(f"/cluster/replication/policies/{pid}", node=node)
        if action == "events":
            params_q = {"limit": _g(params, "limit", 50), "policyId": _g(params, "policy_id")}
            return client.get("/cluster/replication/events", params=params_q, node=node)
        if action == "event_progress":
            event_id = _g(params, "event_id")
            if event_id is None:
                raise SylveError("'event_id' is required.")
            return client.get(
                f"/cluster/replication/events/{event_id}/progress",
                params={"nodeId": _g(params, "node")},
            )
        if action == "failover":
            pid = _g(params, "policy_id")
            target = str(_g(params, "target_node") or "")
            if pid is None or not target:
                raise SylveError("'policy_id' and 'target_node' are required.")
            body = {
                "targetNodeId": target,
                "mode": _g(params, "mode") or "safe",
                "movePinnedSource": _bool(params, "move_pinned_source", True),
                "confirmDataLoss": _bool(params, "confirm_data_loss", False),
            }
            resp = client.post(f"/cluster/replication/policies/{pid}/failover", json_body=body)
            return dict(_maybe_wait(client, resp, params))
        raise SylveError(
            "action must be policies, policy_show, events, event_progress, or failover."
        )


def _backup(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "targets").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "targets":
            return client.get("/cluster/backups/targets", node=node)
        if action == "jobs":
            return client.get(
                "/cluster/backups/jobs",
                params={
                    "targetId": _g(params, "target_id"),
                    "guestType": _g(params, "guest_type"),
                    "guestId": _g(params, "guest_id"),
                },
                node=node,
            )
        if action == "run_job":
            job_id = _g(params, "job_id")
            if job_id is None:
                raise SylveError("'job_id' is required.")
            resp = client.post(f"/cluster/backups/jobs/{job_id}/run", node=node)
            return {"queued": True, "job_id": int(job_id)} | _dict_or_empty(
                _maybe_wait(client, resp, params)
            )
        if action == "events":
            return client.get(
                "/cluster/backups/events",
                params={"limit": _g(params, "limit", 50), "jobId": _g(params, "job_id")},
                node=node,
            )
        if action == "event_progress":
            event_id = _g(params, "event_id")
            if event_id is None:
                raise SylveError("'event_id' is required.")
            return client.get(
                f"/cluster/backups/events/{event_id}/progress",
                params={"nodeId": _g(params, "node")},
            )
        if action == "snapshots":
            job_id = _g(params, "job_id")
            if job_id is None:
                raise SylveError("'job_id' is required.")
            return client.get(f"/cluster/backups/jobs/{job_id}/snapshots", node=node)
        if action == "restore":
            job_id = _g(params, "job_id")
            if job_id is None:
                raise SylveError("'job_id' is required.")
            body = _g(params, "body") or {}
            resp = client.post(f"/cluster/backups/jobs/{job_id}/restore", json_body=body, node=node)
            return dict(_maybe_wait(client, resp, params))
        raise SylveError(
            "action must be targets, jobs, run_job, events, event_progress, snapshots, or restore."
        )


def _task(params: dict[str, Any], settings: MCPSettings) -> Any:
    action = str(_g(params, "action") or "recent").lower()
    with _client_for(params, settings) as client:
        node = _g(params, "node")
        if action == "active":
            rows = []
            for host, data in client.fan_out_get("/tasks/lifecycle/active", node=node):
                rows.extend(_fanned_rows(data, host))
            return rows
        if action == "recent":
            rows = []
            for host, data in client.fan_out_get(
                "/tasks/lifecycle/recent",
                params={"limit": _g(params, "limit", 25)},
                node=node,
            ):
                rows.extend(_fanned_rows(data, host))
            return rows
        if action == "show":
            task_id = _g(params, "task_id")
            if task_id is None:
                raise SylveError("'task_id' is required.")
            task = get_lifecycle_task(client, int(task_id), node=node)
            if task is None:
                raise SylveError(f"Lifecycle task #{task_id} not found.")
            return task
        if action == "wait":
            task_id = _g(params, "task_id")
            if task_id is None:
                raise SylveError("'task_id' is required.")
            timeout = float(_g(params, "timeout", 60.0))
            return wait_for_lifecycle_task(client, int(task_id), node=node, timeout=timeout)
        if action == "cancel_migration":
            task_id = _g(params, "task_id")
            if task_id is None:
                raise SylveError("'task_id' is required.")
            client.post(f"/tasks/migration/{task_id}/cancel", node=node)
            return {"cancellation_requested": True, "task_id": int(task_id)}
        raise SylveError("action must be recent, active, show, wait, or cancel_migration.")


# ---------------------------------------------------------------------------
# Universal OpenAPI tools
# ---------------------------------------------------------------------------
def _api_search(params: dict[str, Any], settings: MCPSettings, spec: OpenAPI) -> Any:
    return spec.find(
        query=_g(params, "query"),
        tag=_g(params, "tag"),
        method=_g(params, "method"),
        limit=int(_g(params, "limit", 50)),
    )


def _api_schema(params: dict[str, Any], settings: MCPSettings, spec: OpenAPI) -> Any:
    method = str(_g(params, "method") or "").upper()
    path = str(_g(params, "path") or "").strip()
    if not method or not path:
        raise SylveError("'method' and 'path' are required. Use sylve_api_search first.")
    try:
        method, template, _op = spec.plan({"method": method, "path": path})
    except ValueError as exc:
        raise SylveError(str(exc)) from exc
    return spec.operation_schema(method, template)


def _api_call_tool(params: dict[str, Any], settings: MCPSettings, spec: OpenAPI) -> Any:
    return _api_call(params, settings, spec)


# ---------------------------------------------------------------------------
# Registration entry points
# ---------------------------------------------------------------------------
def register_tools(server: MCPServer, settings: MCPSettings) -> None:
    """Register all curated cluster-aware tools."""
    _register(
        server,
        settings,
        "sylve_whoami",
        "Show the active Sylve login context, user, node, and JWT expiry status.",
        _schema({}, ["context"]),
        _whoami,
        _ann(title="Sylve whoami", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_context_list",
        "List configured Sylve login contexts from ~/.config/sylve/config.yaml.",
        _schema({}),
        _context_list,
        _ann(title="Sylve contexts", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_cluster_status",
        "Show cluster status: Raft leader, quorum members, node health, and guest counts.",
        _schema({}),
        _cluster_status,
        _ann(title="Cluster status", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_cluster_resources",
        "List every VM, Jail, and template across all cluster nodes.",
        _schema({}),
        _cluster_resources,
        _ann(title="Cluster resources", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_node_list",
        "List all cluster nodes with CPU%, RAM%, disk, guest counts, and version.",
        _schema({}),
        _node_list,
        _ann(title="Node list", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_node_show",
        "Show detailed CPU/RAM/swap/disk telemetry for a node (defaults to the connected node).",
        _schema(
            {
                "node": {
                    "type": "string",
                    "description": "Node hostname or UUID (defaults to current).",
                }
            }
        ),
        _node_show,
        _ann(title="Node show", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_node_audit_logs",
        "Show recent audit records from a node (or all nodes) with parsed request paths.",
        _schema({}),
        _node_audit_logs,
        _ann(title="Node audit logs", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_node_drain",
        "Build and optionally execute a maintenance drain plan for a node (HA failover, "
        "migration, or stop). Dry run by default for safety.",
        _schema(
            {
                "node": {
                    "type": "string",
                    "description": "Node hostname or UUID to drain (required).",
                },
                "target_node": {"type": "string", "description": "Explicit destination node."},
                "stop_only": {
                    "type": "boolean",
                    "description": "Stop guests instead of migrating.",
                },
                "fallback_stop": {
                    "type": "boolean",
                    "description": "Stop guests that cannot migrate.",
                },
                "running_only": {"type": "boolean", "description": "Skip stopped guests."},
                "dry_run": {
                    "type": "boolean",
                    "description": "Only build the plan (default true).",
                },
                "ignore_errors": {
                    "type": "boolean",
                    "description": "Continue past failing guests.",
                },
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["node"],
        ),
        _node_drain,
        _ann(title="Node drain", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_list",
        "List Bhyve VMs across the cluster.",
        _schema(
            {
                "detailed": {
                    "type": "boolean",
                    "description": "Fetch full details (vCPUs, RAM, autostart).",
                }
            }
        ),
        _vm_list,
        _ann(title="List VMs", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_show",
        "Show a VM's full configuration, live state, and guest-agent IP addresses.",
        _schema({"vm": {"type": "string", "description": "VM RID or name."}}, ["vm"]),
        _vm_show,
        _ann(title="Show VM", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_create",
        "Provision a Bhyve VM from scratch or from a template. Parses sizes like '2G', "
        "auto-allocs RID and VNC port, validates cloud-init YAML. Supports dry_run.",
        _schema(
            {
                "name": {"type": "string", "description": "VM name."},
                "template": {
                    "type": "string",
                    "description": "Create from a VM template ID or name.",
                },
                "description": {"type": "string"},
                "memory": {"type": "string", "description": "RAM, e.g. '2G' or '4096M'."},
                "cores": {
                    "type": "integer",
                    "description": "CPU cores (sockets/threads default 1).",
                },
                "sockets": {"type": "integer"},
                "threads": {"type": "integer"},
                "storage_type": {
                    "type": "string",
                    "enum": ["zvol", "raw", "image", "filesystem", "none"],
                },
                "pool": {"type": "string", "description": "ZFS pool for the primary disk."},
                "disk_size": {"type": "string", "description": "Primary disk size, e.g. '20G'."},
                "storage_emulation": {
                    "type": "string",
                    "enum": ["virtio-blk", "virtio-9p", "ahci-hd", "ahci-cd", "nvme"],
                },
                "iso": {"type": "string", "description": "ISO/download UUID or filename."},
                "switch": {"type": "string", "description": "Network switch name, or 'none'."},
                "switch_emulation": {"type": "string", "enum": ["virtio", "e1000"]},
                "mac_object": {"type": "string", "description": "Existing MAC object name or ID."},
                "cloud_init": {"type": "boolean"},
                "user_data": {
                    "type": "string",
                    "description": "cloud-init user-data YAML (inline or path).",
                },
                "meta_data": {
                    "type": "string",
                    "description": "cloud-init meta-data YAML (inline or path).",
                },
                "network_config": {
                    "type": "string",
                    "description": "cloud-init network-config YAML (inline or path).",
                },
                "vnc": {
                    "type": "boolean",
                    "description": "Enable VNC (default true, allocates a port).",
                },
                "vnc_port": {"type": "integer"},
                "vnc_password": {"type": "string"},
                "serial": {"type": "boolean"},
                "tpm": {"type": "boolean"},
                "qemu_guest_agent": {"type": "boolean"},
                "boot_rom": {"type": "string", "enum": ["uefi", "uboot", "none"]},
                "time_offset": {"type": "string", "enum": ["utc", "localtime"]},
                "start_at_boot": {"type": "boolean"},
                "start_order": {"type": "integer"},
                "pci_devices": {"type": "array", "items": {"type": "string"}},
                "extra_bhyve_options": {"type": "array", "items": {"type": "string"}},
                "rid": {"type": "integer", "description": "Explicit RID (defaults to next free)."},
                "start_rid": {"type": "integer", "description": "Template mode: first RID."},
                "count": {"type": "integer", "description": "Template mode: number of VMs."},
                "name_prefix": {"type": "string", "description": "Template mode: name prefix."},
                "storage_pools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Template mode: SOURCE_ID=POOL mappings.",
                },
                "rewrite_cloud_init_identity": {"type": "boolean"},
                "cloud_init_prefix": {"type": "string"},
                "start": {"type": "boolean", "description": "Start the VM(s) after creation."},
                "dry_run": {
                    "type": "boolean",
                    "description": "Print the request without creating.",
                },
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
                "config": {
                    "type": "object",
                    "description": "Optional create config dict mirroring 'sylve vm create --file' keys.",
                },
            }
        ),
        _vm_create,
        _ann(title="Create VM", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_action",
        "Start, stop, shutdown, reboot, or restart one or more VMs by name or RID.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "shutdown", "reboot", "restart"],
                },
                "vms": {"type": "array", "items": {"type": "string"}},
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["action", "vms"],
        ),
        _vm_action,
        _ann(title="VM action", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_migrate",
        "Validate and live-migrate a VM to another cluster node.",
        _schema(
            {
                "vm": {"type": "string", "description": "VM RID or name."},
                "target": {"type": "string", "description": "Target node hostname or UUID."},
                "validate_only": {"type": "boolean", "description": "Run preflight only."},
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["vm", "target"],
        ),
        _vm_migrate,
        _ann(title="Migrate VM", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_delete",
        "Delete a VM and optionally its volumes, raw disks, and MAC objects.",
        _schema(
            {
                "vm": {"type": "string", "description": "VM RID or name."},
                "delete_volumes": {"type": "boolean", "default": True},
                "delete_raw_disks": {"type": "boolean", "default": True},
                "delete_macs": {"type": "boolean", "default": True},
                "force": {"type": "boolean", "default": False},
            },
            ["vm"],
        ),
        _vm_delete,
        _ann(title="Delete VM", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_logs",
        "Retrieve a VM's serial console log output.",
        _schema({"vm": {"type": "string", "description": "VM RID or name."}}, ["vm"]),
        _vm_logs,
        _ann(title="VM logs", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_snapshot",
        "List, create, roll back, or delete VM ZFS snapshots.",
        _schema(
            {
                "action": {"type": "string", "enum": ["list", "create", "rollback", "delete"]},
                "vm": {"type": "string", "description": "VM RID or name."},
                "name": {"type": "string", "description": "Snapshot name (create)."},
                "description": {"type": "string", "description": "Snapshot description (create)."},
                "snapshot_id": {"type": "integer", "description": "Snapshot ID (rollback/delete)."},
            },
            ["action", "vm"],
        ),
        _vm_snapshot,
        _ann(title="VM snapshots", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_vm_template",
        "List, show, capture, deploy, or delete VM templates.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "show", "capture", "deploy", "delete"],
                },
                "vm": {"type": "string", "description": "Source VM (capture)."},
                "name": {"type": "string", "description": "Template name (capture)."},
                "template_id": {
                    "type": "integer",
                    "description": "Template ID (show/deploy/delete).",
                },
                "count": {"type": "integer", "description": "Number of VMs (deploy)."},
            },
            ["action"],
        ),
        _vm_template,
        _ann(title="VM templates", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_list",
        "List FreeBSD Jails across the cluster.",
        _schema({}),
        _jail_list,
        _ann(title="List jails", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_show",
        "Show a Jail's full configuration and live guest info.",
        _schema({"jail": {"type": "string", "description": "Jail CTID or name."}}, ["jail"]),
        _jail_show,
        _ann(title="Show jail", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_create",
        "Create a FreeBSD Jail on a ZFS pool.",
        _schema(
            {
                "name": {"type": "string"},
                "pool": {"type": "string", "description": "ZFS pool name."},
                "type": {"type": "string", "default": "freebsd"},
                "hostname": {"type": "string"},
                "base": {"type": "string"},
                "bootstrap_name": {"type": "string"},
                "cores": {"type": "integer"},
                "memory": {"type": "string", "description": "RAM limit, e.g. '1G'."},
                "ipv4_raw": {"type": "string"},
                "ipv4_gw_raw": {"type": "string"},
                "ipv6_raw": {"type": "string"},
                "ipv6_gw_raw": {"type": "string"},
                "switch_name": {"type": "string"},
                "dhcp": {"type": "boolean"},
                "start_at_boot": {"type": "boolean"},
                "start_order": {"type": "integer"},
                "description": {"type": "string"},
            },
            ["name", "pool"],
        ),
        _jail_create,
        _ann(title="Create jail", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_action",
        "Start, stop, or restart one or more Jails by CTID or name.",
        _schema(
            {
                "action": {"type": "string", "enum": ["start", "stop", "restart"]},
                "jails": {"type": "array", "items": {"type": "string"}},
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["action", "jails"],
        ),
        _jail_action,
        _ann(title="Jail action", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_migrate",
        "Validate and migrate a Jail to another cluster node.",
        _schema(
            {
                "jail": {"type": "string", "description": "Jail CTID or name."},
                "target": {"type": "string", "description": "Target node hostname or UUID."},
                "validate_only": {"type": "boolean"},
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["jail", "target"],
        ),
        _jail_migrate,
        _ann(title="Migrate jail", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_delete",
        "Delete a Jail, optionally removing its rootfs and MAC objects.",
        _schema(
            {
                "jail": {"type": "string", "description": "Jail CTID or name."},
                "delete_rootfs": {"type": "boolean", "default": True},
                "delete_macs": {"type": "boolean", "default": True},
            },
            ["jail"],
        ),
        _jail_delete,
        _ann(title="Delete jail", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_logs",
        "Retrieve a Jail's console log output.",
        _schema({"jail": {"type": "string", "description": "Jail CTID or name."}}, ["jail"]),
        _jail_logs,
        _ann(title="Jail logs", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_snapshot",
        "List, create, roll back, or delete Jail ZFS snapshots.",
        _schema(
            {
                "action": {"type": "string", "enum": ["list", "create", "rollback", "delete"]},
                "jail": {"type": "string", "description": "Jail CTID or name."},
                "name": {"type": "string", "description": "Snapshot name (create)."},
                "description": {"type": "string"},
                "snapshot_id": {"type": "integer", "description": "Snapshot ID (rollback/delete)."},
            },
            ["action", "jail"],
        ),
        _jail_snapshot,
        _ann(title="Jail snapshots", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_bootstrap",
        "List, create, or delete pkgbase FreeBSD jail bootstraps on a ZFS pool.",
        _schema(
            {
                "action": {"type": "string", "enum": ["list", "create", "delete"]},
                "pool": {"type": "string", "description": "ZFS pool name."},
                "major": {"type": "integer", "description": "FreeBSD major version (create)."},
                "minor": {"type": "integer", "default": 0},
                "type": {"type": "string", "default": "base"},
                "name": {"type": "string", "description": "Bootstrap name (delete)."},
            },
            ["action"],
        ),
        _jail_bootstrap,
        _ann(title="Jail bootstraps", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_jail_template",
        "List, show, capture, deploy, or delete jail templates.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "show", "capture", "deploy", "delete"],
                },
                "jail": {"type": "string", "description": "Source jail (capture)."},
                "name": {"type": "string", "description": "Template name (capture)."},
                "template_id": {"type": "integer"},
                "count": {"type": "integer"},
            },
            ["action"],
        ),
        _jail_template,
        _ann(title="Jail templates", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_zfs_pool",
        "List, inspect, or scrub ZFS storage pools (resolves pool names to GUIDs).",
        _schema(
            {
                "action": {"type": "string", "enum": ["list", "status", "scrub", "disks_usage"]},
                "pool": {"type": "string", "description": "Pool name or GUID (status/scrub)."},
            },
            ["action"],
        ),
        _zfs_pool,
        _ann(title="ZFS pools", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_zfs_dataset",
        "List, create, edit, or delete ZFS filesystems and volumes.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "create_filesystem", "create_volume", "edit", "delete"],
                },
                "type": {
                    "type": "string",
                    "description": "List filter or delete/edit kind (filesystem|volume).",
                },
                "parent": {"type": "string", "description": "Parent dataset path (create)."},
                "name": {"type": "string", "description": "Dataset name (create)."},
                "size": {
                    "type": "string",
                    "description": "Volume size, e.g. '10G' (create_volume).",
                },
                "properties": {"type": "object"},
                "dataset": {"type": "string", "description": "Dataset name or GUID (edit/delete)."},
            },
            ["action"],
        ),
        _zfs_dataset,
        _ann(title="ZFS datasets", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_zfs_snapshot",
        "List, create, roll back, or delete ZFS snapshots, plus periodic snapshot jobs.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "rollback", "delete", "periodic_list"],
                },
                "snapshot": {
                    "type": "string",
                    "description": "Snapshot name (dataset@snap) or GUID.",
                },
                "dataset": {"type": "string", "description": "Alias for snapshot."},
                "name": {"type": "string", "description": "Snapshot name after @ (create)."},
                "recursive": {"type": "boolean"},
                "destroy_more_recent": {
                    "type": "boolean",
                    "description": "Rollback: destroy newer snapshots.",
                },
            },
            ["action"],
        ),
        _zfs_snapshot,
        _ann(title="ZFS snapshots", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_download",
        "List, show, queue URL downloads, or delete ISOs/images on the downloader.",
        _schema(
            {
                "action": {"type": "string", "enum": ["list", "show", "add", "delete"]},
                "identifier": {
                    "type": "string",
                    "description": "Download ID, UUID, or name (show).",
                },
                "url": {"type": "string", "description": "URL/magnet/server path (add)."},
                "filename": {"type": "string"},
                "download_type": {"type": "string", "default": "uncategorized"},
                "storage_pool": {"type": "string"},
                "extract": {"type": "boolean", "default": True},
                "raw_convert": {"type": "boolean", "default": False},
                "ignore_tls": {"type": "boolean", "default": False},
                "ids": {"type": "array", "items": {"type": "integer"}},
            },
            ["action"],
        ),
        _download,
        _ann(title="Downloads", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_network_overview",
        "Query one network subsystem (interfaces, switches, routes, DHCP, firewall, objects, WireGuard).",
        _schema(
            {
                "kind": {
                    "type": "string",
                    "enum": [
                        "interfaces",
                        "switches",
                        "routes",
                        "dhcp",
                        "firewall_traffic",
                        "firewall_nat",
                        "firewall_counters",
                        "firewall_advanced",
                        "firewall_rendered",
                        "objects",
                        "wireguard",
                        "global_config",
                        "all",
                    ],
                }
            }
        ),
        _network_overview,
        _ann(title="Network overview", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_replication",
        "Manage HA replication policies, events, and failovers.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["policies", "policy_show", "events", "event_progress", "failover"],
                },
                "policy_id": {"type": "integer"},
                "event_id": {"type": "integer"},
                "target_node": {"type": "string", "description": "Failover target node ID."},
                "mode": {"type": "string", "default": "safe"},
                "move_pinned_source": {"type": "boolean", "default": True},
                "confirm_data_loss": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 50},
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["action"],
        ),
        _replication,
        _ann(title="Replication", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_backup",
        "Inspect backup targets, jobs, and events; run or restore backups.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "targets",
                        "jobs",
                        "run_job",
                        "events",
                        "event_progress",
                        "snapshots",
                        "restore",
                    ],
                },
                "job_id": {"type": "integer"},
                "event_id": {"type": "integer"},
                "target_id": {"type": "integer"},
                "guest_type": {"type": "string"},
                "guest_id": {"type": "integer"},
                "body": {"type": "object", "description": "Restore request body."},
                "limit": {"type": "integer", "default": 50},
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["action"],
        ),
        _backup,
        _ann(title="Backups", destructive=True),
    )
    _register(
        server,
        settings,
        "sylve_task",
        "List, show, or wait on guest lifecycle tasks; cancel migrations.",
        _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["recent", "active", "show", "wait", "cancel_migration"],
                },
                "task_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 25},
                "timeout": {"type": "number", "default": 60.0},
            },
            ["action"],
        ),
        _task,
        _ann(title="Tasks", destructive=True),
    )


def register_openapi_tools(
    server: MCPServer,
    settings: MCPSettings,
    spec: OpenAPI,
) -> None:
    """Register the three universal Swagger tools."""

    def _search_handler(params: dict[str, Any], _settings: MCPSettings) -> Any:
        return _api_search(params, _settings, spec)

    def _schema_handler(params: dict[str, Any], _settings: MCPSettings) -> Any:
        return _api_schema(params, _settings, spec)

    def _call_handler(params: dict[str, Any], _settings: MCPSettings) -> Any:
        return _api_call_tool(params, _settings, spec)

    _register(
        server,
        settings,
        "sylve_api_search",
        "Search all Sylve Swagger operations by keyword, tag, or HTTP method. Use this to "
        "discover any of the 441 endpoints before calling them.",
        _schema(
            {
                "query": {
                    "type": "string",
                    "description": "Free-text search across paths and summaries.",
                },
                "tag": {
                    "type": "string",
                    "description": "Swagger tag, e.g. VM, Jail, ZFS, Network.",
                },
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "limit": {"type": "integer", "default": 50},
            }
        ),
        _search_handler,
        _ann(title="API search", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_api_schema",
        "Show the full request/response schema (with $refs resolved) for one Swagger operation. "
        "Use sylve_api_search to find the operation first.",
        _schema(
            {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "description": "API path, e.g. /vm/{rid}/networks."},
            },
            ["method", "path"],
        ),
        _schema_handler,
        _ann(title="API schema", read_only=True, idempotent=True),
    )
    _register(
        server,
        settings,
        "sylve_api_call",
        "Execute any documented Sylve API operation directly (GET/POST/PUT/PATCH/DELETE). "
        "Combine with sylve_api_schema to build accurate bodies. Filters out None query params.",
        _schema(
            {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {
                    "type": "string",
                    "description": "API path, e.g. /vm/{rid}/actions/{action}.",
                },
                "path_params": {
                    "type": "object",
                    "description": "Values for {placeholders} in the path.",
                },
                "query_params": {"type": "object"},
                "body": {"type": "object"},
                "upload_file": {
                    "type": "string",
                    "description": "Local file to multipart-upload (POST).",
                },
                "download_path": {
                    "type": "string",
                    "description": "Local path to stream a binary download to.",
                },
                "event_kind": {
                    "type": "string",
                    "enum": ["replication", "backup"],
                    "description": "Poll returned eventId as this kind.",
                },
                "wait": {"type": "boolean", "default": True},
                "wait_timeout": {"type": "number", "default": 60.0},
            },
            ["method", "path"],
        ),
        _call_handler,
        _ann(title="Call API endpoint"),
    )


def register_dynamic_tools(
    server: MCPServer,
    settings: MCPSettings,
    spec: OpenAPI,
    tags: set[str] | None = None,
) -> None:
    """Register one MCP tool per Swagger operation (opt-in via --expose-tags/--expose-all)."""
    for desc in spec.dynamic_tool_specs(tags):
        server.register_tool(
            desc["name"],
            desc["description"],
            desc["input_schema"],
            _bind_dynamic(desc, settings, spec),
            annotations=_ann(title=desc["name"]),
        )


def _bind_dynamic(
    desc: dict[str, Any],
    settings: MCPSettings,
    spec: OpenAPI,
) -> Callable[[dict[str, Any]], Any]:
    def _call(params: dict[str, Any]) -> Any:
        return _api_call_from_descriptor(desc, params, settings, spec)

    return _call


def register_resources_and_prompts(server: MCPServer, settings: MCPSettings, spec: OpenAPI) -> None:
    """Register live-cluster MCP resources and operational prompts."""

    def _cluster_status_text() -> str:
        payload = _cluster_status({}, settings)
        return json.dumps(payload, indent=2, default=str)

    def _cluster_resources_text() -> str:
        payload = _cluster_resources({}, settings)
        return json.dumps(payload, indent=2, default=str)

    server.register_resource(
        "sylve://cluster/status",
        "Cluster status",
        _cluster_status_text,
        description="Live Sylve cluster and Raft status JSON.",
    )
    server.register_resource(
        "sylve://cluster/resources",
        "Cluster resources",
        _cluster_resources_text,
        description="All VMs, Jails, and templates across cluster nodes.",
    )
    server.register_resource(
        "sylve://openapi/tags",
        "OpenAPI tag summary",
        lambda: json.dumps(spec.tags_summary(), indent=2),
        description="All Swagger tags and their operation counts.",
    )

    server.register_prompt(
        "triage-cluster",
        "Inspect cluster health: offline nodes, failed tasks, and ZFS pool status.",
        lambda _args: (
            "Diagnose the Sylve cluster's health. Run these steps and report findings:\n"
            "1. sylve_cluster_status - check quorum and node health.\n"
            "2. sylve_node_list - identify offline or draining nodes.\n"
            "3. sylve_task action='recent' - look for failed or stuck lifecycle tasks.\n"
            "4. sylve_zfs_pool action='list' and action='status' for each degraded pool.\n"
            "5. For any failing guest, inspect sylve_vm_show / sylve_jail_show and logs.\n"
            "Summarize the issues, impact, and suggested remediation steps."
        ),
    )
    server.register_prompt(
        "plan-node-drain",
        "Build a dry-run maintenance drain plan for a node and flag blocked guests or pinned backup jobs.",
        lambda args: (
            "Plan maintenance draining for Sylve node "
            f"'{args.get('node', '<node>')}'. Call sylve_node_drain with dry_run=true, "
            "review every item's action (ha-failover, migrate, shutdown, stop, blocked), "
            "and report any blocked guests, the pinned backup jobs that reference the node, "
            "and the least-loaded destination node if no --target was given."
        ),
    )
