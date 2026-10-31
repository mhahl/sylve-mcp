"""Sylve API HTTP client with cluster-wide routing and guest-to-node resolution.

This is a trimmed port of the sylve-cli client (same URL scheme, headers, and
envelope handling) with two operations-safety additions: an optional read-only
guard that refuses every non-GET request, and a guard that turns the
SvelteKit "HTML catch-all" (returned by Sylve for unreleased routes) into a
clean error instead of raw HTML flowing back to the model.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from sylve_mcp.config import ContextConfig, normalize_server_url

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class SylveError(Exception):
    """Base exception for Sylve MCP errors."""


class SessionExpiredError(SylveError):
    """Raised when the stored JWT token is missing, expired, or rejected with 401."""


class SylveAPIError(SylveError):
    """Raised when the Sylve API returns an error status or non-2xx HTTP code."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_code: str = "",
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.data = data


class ResourceNotFoundError(SylveError):
    """Raised when a VM, Jail, Node, or other resource cannot be found."""


class SylveClient:
    """HTTP client for the Sylve API with multi-node cluster support."""

    def __init__(
        self,
        context: ContextConfig,
        node_override: str | None = None,
        timeout: float = 30.0,
        read_only: bool = False,
    ) -> None:
        self.context = context
        self.node_override = node_override
        self.timeout = timeout
        self.read_only = read_only
        self._base_url = f"{normalize_server_url(context.server)}/api"

        verify: bool | str = context.verify_ssl
        if context.verify_ssl and context.ca_cert:
            verify = str(Path(context.ca_cert).expanduser())

        self._http = httpx.Client(
            base_url=self._base_url,
            verify=verify,
            timeout=timeout,
            follow_redirects=True,
        )
        self._nodes_cache: list[dict[str, Any]] | None = None
        self._resources_cache: list[dict[str, Any]] | None = None

    def close(self) -> None:
        """Close the underlying HTTPX client."""
        self._http.close()

    def __enter__(self) -> SylveClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _ensure_valid_token(self) -> None:
        if not self.context.token:
            raise SessionExpiredError(
                "No authentication token found. Please log in with 'sylve login <server-url>'."
            )
        if self.context.is_token_expired():
            expiry = self.context.get_expiry_datetime()
            exp_str = expiry.strftime("%Y-%m-%d %H:%M:%S UTC") if expiry else "unknown time"
            raise SessionExpiredError(
                f"Authentication token expired at {exp_str}. "
                "Please re-authenticate with 'sylve login'."
            )

    def resolve_node_hostname(self, node_identifier: str | None) -> str | None:
        """Resolve a node identifier to a canonical hostname (or None for cluster-wide)."""
        target = node_identifier or self.node_override or self.context.default_node
        if not target:
            return None
        target = target.strip()
        if not target or target.lower() == "all":
            return None

        try:
            nodes = self.get_cluster_nodes()
        except SylveError:
            return target

        for n in nodes:
            hostname = str(n.get("hostname") or "")
            uuid = str(n.get("nodeUUID") or n.get("nodeUuid") or n.get("id") or "")
            if target.lower() in (hostname.lower(), uuid.lower()):
                return hostname or target
        return target

    def resolve_node_uuid(self, node_identifier: str) -> tuple[str, str]:
        """Resolve a node identifier to ``(nodeUUID, hostname)``."""
        target = node_identifier.strip()
        nodes = self.get_cluster_nodes()
        for n in nodes:
            hostname = str(n.get("hostname") or "")
            uuid = str(n.get("nodeUUID") or n.get("nodeUuid") or "")
            if target.lower() in (hostname.lower(), uuid.lower()):
                return uuid, hostname
        raise ResourceNotFoundError(
            f"Cluster node '{node_identifier}' not found. "
            f"Available nodes: {', '.join(str(n.get('hostname')) for n in nodes)}"
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        node: str | None = None,
        require_auth: bool = True,
        unwrap_data: bool = True,
        resolve_node: bool = True,
    ) -> Any:
        """Send an HTTP request and unwrap the APIResponse envelope."""
        method = method.upper()
        if method not in HTTP_METHODS:
            raise SylveError(f"Unsupported HTTP method '{method}'.")
        if self.read_only and method != "GET":
            raise SylveError(
                f"Server is running in read-only mode; refusing to send {method} {path}."
            )

        req_headers: dict[str, str] = {"Accept": "application/json"}
        if require_auth:
            self._ensure_valid_token()
            req_headers["Authorization"] = f"Bearer {self.context.token}"

        resolved_host = self.resolve_node_hostname(node) if require_auth and resolve_node else None
        if resolved_host:
            req_headers["X-Current-Hostname"] = resolved_host

        if headers:
            req_headers.update(headers)

        clean_params: dict[str, Any] | None = None
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}

        try:
            resp = self._http.request(
                method=method,
                url=path,
                params=clean_params,
                json=json_body,
                files=files,
                headers=req_headers,
            )
        except httpx.RequestError as exc:
            raise SylveError(f"Connection error talking to {self.context.server}: {exc}") from exc

        if resp.status_code == 401 and require_auth:
            raise SessionExpiredError(
                "Server rejected authentication token (HTTP 401 Unauthorized). "
                "Please re-authenticate with 'sylve login'."
            )

        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if resp.status_code < 400 and content_type == "text/html" and (not json_body or True):
            # The SvelteKit catch-all answers HTML for unreleased routes
            # (documented in sylve-cli docs/spec-drift.md).
            raise SylveAPIError(
                f"Endpoint {method} {path} returned an HTML page instead of JSON; "
                "it is not implemented on this server build. "
                "Run 'sylve_api_search' or check the server version.",
                status_code=resp.status_code,
                error_code="endpoint_not_implemented",
            )

        payload: Any = None
        if resp.content:
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text

        if resp.status_code >= 400:
            err_msg = f"HTTP {resp.status_code}"
            err_code = ""
            err_data = None
            if isinstance(payload, dict):
                err_code = str(payload.get("error") or payload.get("message") or "")
                err_msg = str(payload.get("message") or payload.get("error") or err_msg)
                err_data = payload.get("data")
                if err_code and err_code != err_msg:
                    err_msg = f"{err_msg} ({err_code})"
            elif isinstance(payload, str) and payload.strip():
                err_msg = payload.strip()
            raise SylveAPIError(
                message=err_msg,
                status_code=resp.status_code,
                error_code=err_code,
                data=err_data,
            )

        if isinstance(payload, dict) and payload.get("status") == "error":
            err_code = str(payload.get("error") or "")
            err_msg = str(payload.get("message") or err_code or "API Error")
            raise SylveAPIError(
                message=err_msg,
                status_code=resp.status_code,
                error_code=err_code,
                data=payload.get("data"),
            )

        if unwrap_data and isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        node: str | None = None,
        unwrap_data: bool = True,
    ) -> Any:
        return self.request("GET", path, params=params, node=node, unwrap_data=unwrap_data)

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
        node: str | None = None,
        unwrap_data: bool = True,
    ) -> Any:
        return self.request(
            "POST",
            path,
            params=params,
            json_body=json_body,
            files=files,
            node=node,
            unwrap_data=unwrap_data,
        )

    def put(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        node: str | None = None,
        unwrap_data: bool = True,
    ) -> Any:
        return self.request(
            "PUT",
            path,
            params=params,
            json_body=json_body,
            node=node,
            unwrap_data=unwrap_data,
        )

    def patch(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        node: str | None = None,
        unwrap_data: bool = True,
    ) -> Any:
        return self.request(
            "PATCH",
            path,
            params=params,
            json_body=json_body,
            node=node,
            unwrap_data=unwrap_data,
        )

    def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        node: str | None = None,
        unwrap_data: bool = True,
    ) -> Any:
        return self.request(
            "DELETE",
            path,
            params=params,
            json_body=json_body,
            node=node,
            unwrap_data=unwrap_data,
        )

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        params: dict[str, Any] | None = None,
        node: str | None = None,
    ) -> Path:
        """Stream a binary response to a local file."""
        if self.read_only:
            raise SylveError(f"Server is running in read-only mode; refusing to download {path}.")
        self._ensure_valid_token()
        headers = {"Accept": "*/*", "Authorization": f"Bearer {self.context.token}"}
        resolved_host = self.resolve_node_hostname(node)
        if resolved_host:
            headers["X-Current-Hostname"] = resolved_host
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        target = destination.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._http.stream(
            "GET", path, params=clean_params or None, headers=headers
        ) as response:
            if response.status_code >= 400:
                raise SylveError(f"HTTP {response.status_code} downloading {path}.")
            with target.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return target

    # ------------------------------------------------------------------
    # Cluster & multi-node discovery
    # ------------------------------------------------------------------
    def get_cluster_nodes(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Return cluster nodes, falling back to local node information."""
        if self._nodes_cache is not None and not refresh:
            return self._nodes_cache

        nodes: list[dict[str, Any]] = []
        try:
            raw = self.request("GET", "/cluster/nodes", node=None, resolve_node=False)
            if isinstance(raw, list):
                nodes = [n for n in raw if isinstance(n, dict)]
        except SylveError:
            nodes = []

        if not nodes:
            try:
                info = self.request("GET", "/info/node", node=None, resolve_node=False)
                if isinstance(info, dict):
                    nodes = [
                        {
                            "nodeUUID": self.context.node_id or "local",
                            "hostname": info.get("hostname")
                            or self.context.hostname
                            or "localhost",
                            "status": "online",
                            "guestIDs": info.get("guestIds") or [],
                            "sylveVersion": info.get("sylveVersion") or "",
                        }
                    ]
            except SylveError:
                if self.context.hostname:
                    nodes = [
                        {
                            "nodeUUID": self.context.node_id or "local",
                            "hostname": self.context.hostname,
                            "status": "online",
                        }
                    ]

        self._nodes_cache = nodes
        return nodes

    def get_online_nodes(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Return online cluster nodes (falling back to all when none are online)."""
        nodes = self.get_cluster_nodes(refresh=refresh)
        online = [n for n in nodes if str(n.get("status") or "online").strip().lower() == "online"]
        return online or nodes

    def get_cluster_resources(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Return per-node VM, Jail, and template resources."""
        if self._resources_cache is not None and not refresh:
            return self._resources_cache

        resources: list[dict[str, Any]] = []
        try:
            raw = self.request("GET", "/cluster/resources", node=None)
            if isinstance(raw, list):
                resources = [r for r in raw if isinstance(r, dict)]
        except SylveError:
            resources = []

        if not resources:
            vms: list[Any] = []
            jails: list[Any] = []
            try:
                v_raw = self.get("/vm/simple")
                if isinstance(v_raw, list):
                    vms = v_raw
            except SylveError:
                pass
            try:
                j_raw = self.get("/jail/simple")
                if isinstance(j_raw, list):
                    jails = j_raw
            except SylveError:
                pass
            hostname = self.context.hostname or "localhost"
            nodes = self.get_cluster_nodes()
            if nodes and nodes[0].get("hostname"):
                hostname = str(nodes[0]["hostname"])
            resources = [
                {
                    "nodeUUID": self.context.node_id or "local",
                    "hostname": hostname,
                    "vms": vms,
                    "jails": jails,
                    "vmTemplates": [],
                    "jailTemplates": [],
                }
            ]

        self._resources_cache = resources
        return resources

    def fan_out_get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        node: str | None = None,
    ) -> list[tuple[str, Any]]:
        """Execute a GET request across one node or all online nodes."""
        target = self.resolve_node_hostname(node)
        if target:
            data = self.get(path, params=params, node=target)
            return [(target, data)]

        online_nodes = self.get_online_nodes()
        if len(online_nodes) <= 1:
            host = (
                str(online_nodes[0].get("hostname"))
                if online_nodes and online_nodes[0].get("hostname")
                else (self.context.hostname or "local")
            )
            data = self.get(path, params=params, node=None)
            return [(host, data)]

        results: list[tuple[int, str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(online_nodes))) as pool:
            futures = {
                pool.submit(self.get, path, params=params, node=str(n.get("hostname"))): (
                    idx,
                    str(n.get("hostname")),
                )
                for idx, n in enumerate(online_nodes)
                if n.get("hostname")
            }
            for fut in as_completed(futures):
                idx, host = futures[fut]
                try:
                    results.append((idx, host, fut.result()))
                except SylveError:
                    continue

        results.sort(key=lambda item: item[0])
        return [(host, data) for _, host, data in results]

    # ------------------------------------------------------------------
    # Guest-to-node resolvers
    # ------------------------------------------------------------------
    def resolve_vm(
        self,
        identifier: str | int,
        node: str | None = None,
    ) -> tuple[int, str, dict[str, Any]]:
        """Resolve a VM by RID or name to ``(rid, hostname, summary)``."""
        ident_str = str(identifier).strip()
        is_numeric = ident_str.isdigit()
        target_host = self.resolve_node_hostname(node)

        matches: list[tuple[int, str, dict[str, Any]]] = []
        for node_res in self.get_cluster_resources():
            host = str(node_res.get("hostname") or "")
            if target_host and host.lower() != target_host.lower():
                continue
            for vm in node_res.get("vms") or []:
                if not isinstance(vm, dict):
                    continue
                rid = vm.get("rid")
                name = str(vm.get("name") or "")
                if (
                    (is_numeric and str(rid) == ident_str) or (name.lower() == ident_str.lower())
                ) and rid is not None:
                    matches.append((int(rid), host, vm))

        if not matches and target_host:
            try:
                vms = self.get("/vm/simple", node=target_host)
                if isinstance(vms, list):
                    for vm in vms:
                        rid = vm.get("rid")
                        name = str(vm.get("name") or "")
                        if (
                            (is_numeric and str(rid) == ident_str)
                            or (name.lower() == ident_str.lower())
                        ) and rid is not None:
                            matches.append((int(rid), target_host, vm))
            except SylveError:
                pass

        if not matches and is_numeric:
            return int(ident_str), target_host or self.context.hostname or "", {}

        if not matches:
            scope = f" on node '{target_host}'" if target_host else " in cluster"
            raise ResourceNotFoundError(f"VM '{identifier}' not found{scope}.")

        if len(matches) > 1:
            locs = ", ".join(f"{m[2].get('name')}(rid={m[0]}, node={m[1]})" for m in matches)
            raise SylveError(
                f"Multiple VMs match '{identifier}': {locs}. Specify the RID or pass 'node'."
            )

        return matches[0]

    def resolve_jail(
        self,
        identifier: str | int,
        node: str | None = None,
    ) -> tuple[int, str, dict[str, Any]]:
        """Resolve a Jail by CTID or name to ``(ctid, hostname, summary)``."""
        ident_str = str(identifier).strip()
        is_numeric = ident_str.isdigit()
        target_host = self.resolve_node_hostname(node)

        matches: list[tuple[int, str, dict[str, Any]]] = []
        for node_res in self.get_cluster_resources():
            host = str(node_res.get("hostname") or "")
            if target_host and host.lower() != target_host.lower():
                continue
            for jail in node_res.get("jails") or []:
                if not isinstance(jail, dict):
                    continue
                ctid = jail.get("ctId")
                name = str(jail.get("name") or "")
                if (
                    (is_numeric and str(ctid) == ident_str) or (name.lower() == ident_str.lower())
                ) and ctid is not None:
                    matches.append((int(ctid), host, jail))

        if not matches and target_host:
            try:
                jails = self.get("/jail/simple", node=target_host)
                if isinstance(jails, list):
                    for jail in jails:
                        ctid = jail.get("ctId")
                        name = str(jail.get("name") or "")
                        if (
                            (is_numeric and str(ctid) == ident_str)
                            or (name.lower() == ident_str.lower())
                        ) and ctid is not None:
                            matches.append((int(ctid), target_host, jail))
            except SylveError:
                pass

        if not matches and is_numeric:
            return int(ident_str), target_host or self.context.hostname or "", {}

        if not matches:
            scope = f" on node '{target_host}'" if target_host else " in cluster"
            raise ResourceNotFoundError(f"Jail '{identifier}' not found{scope}.")

        if len(matches) > 1:
            locs = ", ".join(f"{m[2].get('name')}(ctid={m[0]}, node={m[1]})" for m in matches)
            raise SylveError(
                f"Multiple Jails match '{identifier}': {locs}. Specify the CTID or pass 'node'."
            )

        return matches[0]

    def enrich_template_metadata(
        self, item: dict[str, Any], detail_path: str, node: str | None
    ) -> None:
        """Merge template-detail fields the simple list endpoints omit."""
        try:
            detail = self.get(detail_path, node=node)
        except SylveError:
            return
        if isinstance(detail, dict):
            for key in ("createdAt", "pool"):
                if detail.get(key) is not None:
                    item[key] = detail.get(key)

    # ------------------------------------------------------------------
    # ZFS name-to-GUID resolvers
    # ------------------------------------------------------------------
    def resolve_pool_guid(self, pool_ident: str, node: str | None = None) -> tuple[str, str]:
        """Resolve a ZFS pool name or GUID into ``(pool_guid, node_hostname)``."""
        ident = pool_ident.strip()
        results = self.fan_out_get("/zfs/pools", node=node)
        for host, pools in results:
            if isinstance(pools, list):
                for p in pools:
                    if not isinstance(p, dict):
                        continue
                    guid = str(p.get("pool_guid") or p.get("guid") or "")
                    name = str(p.get("name") or "")
                    if ident.lower() in (guid.lower(), name.lower()):
                        return guid, host
        if ident.isdigit():
            return ident, self.resolve_node_hostname(node) or ""
        raise ResourceNotFoundError(f"ZFS pool '{pool_ident}' not found.")

    def resolve_dataset_guid(
        self,
        dataset_ident: str,
        dtype: str | None = None,
        node: str | None = None,
    ) -> tuple[str, str]:
        """Resolve a ZFS dataset or snapshot name or GUID into ``(guid, node_hostname)``."""
        ident = dataset_ident.strip()
        params = {"type": dtype} if dtype else None
        results = self.fan_out_get("/zfs/datasets", params=params, node=node)
        for host, datasets in results:
            if isinstance(datasets, list):
                for ds in datasets:
                    if not isinstance(ds, dict):
                        continue
                    guid = str(ds.get("guid") or "")
                    name = str(ds.get("name") or "")
                    if ident.lower() in (guid.lower(), name.lower()):
                        return guid, host
        return ident, self.resolve_node_hostname(node) or ""
