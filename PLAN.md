# `sylve-mcp` Implementation Plan

## 1. Overview & Goals

`sylve-mcp` is a Model Context Protocol (MCP) server for **Sylve**, the FreeBSD Bhyve/Jail/ZFS cluster hypervisor. It completes the Sylve automation suite alongside `~/Developer/sylve-cli` and `~/Developer/sylve-ansible`:

- **Same stack as `sylve-cli`**: Python `>=3.10`, `setuptools` PEP 517 build, `httpx>=0.25.0`, `PyYAML>=6.0`, `pytest`, `ruff`, `pylint`, `mypy`, `tox`, `BSD-2-Clause` license, roff manual pages, and a FreeBSD ports `Makefile`.
- **Zero extra runtime dependencies**: Stdlib JSON-RPC 2.0 `stdio` transport implementation so it runs out of the box on FreeBSD with only `www/py-httpx` and `devel/py-pyyaml`.
- **Shared authentication & configuration**: Reads the exact same `~/.config/sylve/config.yaml` (`mode 0600`) contexts created by `sylve login` and honors the same `SYLVE_*` environment variables as `sylve-cli` and `sylve-ansible`.
- **Full API coverage (336 paths / 441 operations)**: Combines curated cluster-aware Dev/Ops tools with a Swagger-driven OpenAPI search/schema/call layer backed by `docs/swagger.json`.

---

## 2. Repository Structure

```text
sylve-mcp/
├── LICENSE                          # BSD-2-Clause
├── Makefile                         # test, lint, format, type, live-test, man, tox
├── MANIFEST.in
├── PLAN.md
├── README.md                        # Usage & MCP client config (OpenCode, Claude, Cursor)
├── TESTING.md
├── pyproject.toml                   # Same build/lint/type/test settings as sylve-cli
├── tox.ini
├── docs/
│   ├── swagger.json                 # Vendored from AlchemillaHQ/Sylve (336 paths, 441 ops)
│   └── man/
│       ├── src/sylve-mcp.1.in
│       └── man1/sylve-mcp.1
├── examples/
│   ├── auth/config.yaml
│   └── mcp/
│       ├── opencode.json
│       └── claude_desktop_config.json
├── packaging/
│   └── freebsd/sysutils/py-sylve-mcp/
│       ├── Makefile
│       └── pkg-descr
├── scripts/
│   └── build_man.py
├── src/sylve_mcp/
│   ├── __init__.py
│   ├── main.py                      # CLI entry point (`sylve-mcp`)
│   ├── config.py                    # Shared ~/.config/sylve/config.yaml + SYLVE_* env loader
│   ├── client.py                    # SylveClient (node routing, guest/pool resolution, HTML guard)
│   ├── tasks.py                     # Bounded lifecycle, replication, and backup task polling
│   ├── openapi.py                   # Swagger 2.0 loader, $ref resolver, search, schema & dynamic tools
│   ├── protocol.py                  # Stdlib MCP JSON-RPC 2.0 stdio server (tools, resources, prompts)
│   └── tools.py                     # Curated Dev/Ops tools + universal OpenAPI tools
└── tests/
    ├── unit/
    │   ├── test_config.py           # Context resolution & env overrides
    │   ├── test_client.py           # Auth, node routing, read-only guard, HTML catch-all guard
    │   ├── test_openapi.py          # 336-path / 441-op coverage & schema resolution
    │   ├── test_protocol.py         # JSON-RPC 2.0 initialize, tools/list, tools/call, resources
    │   └── test_tools.py            # Curated VM, Jail, Node, ZFS, Network, Backup, and API tools
    └── live/
        ├── conftest.py              # Live client & OpenAPI response validator (SYLVE_LIVE=1)
        └── test_live_mcp.py         # End-to-end read-only tool calls against a live Sylve cluster
```

---

## 3. Shared Authentication & Configuration (`src/sylve_mcp/config.py`)

Uses the identical configuration schema and precedence order as `sylve-cli`:

1. **Per-tool call arguments**: Optional `context` (e.g. `prod`, `lab`) and `node` (hostname or UUID) on every MCP tool call.
2. **Server CLI flags**:
   - `--config FILE` — Path to config file.
   - `--context NAME` — Default named context.
   - `-n, --node NODE` — Default target node hostname or UUID.
   - `-k, --insecure` — Skip TLS certificate verification.
   - `--timeout SECONDS` — HTTP request timeout (default: `30.0`).
   - `--read-only` — Block all mutating (`POST`, `PUT`, `PATCH`, `DELETE`) API calls.
   - `--expose-tags TAG1,TAG2` / `--expose-all` — Dynamically register individual MCP tools for every Swagger operation in the selected tags.
3. **Environment variables**:
   - `SYLVE_CONFIG` — Override configuration file path.
   - `SYLVE_CONTEXT` — Override active context name.
   - `SYLVE_SERVER` — Override server URL without a saved context.
   - `SYLVE_TOKEN` — Override bearer token without a saved context.
   - `SYLVE_NODE` — Default target cluster node.
   - `SYLVE_INSECURE` — Set to `1`, `true`, or `yes` to disable TLS verification.
   - `SYLVE_READ_ONLY` — Set to `1`, `true`, or `yes` to enforce read-only mode.
   - `XDG_CONFIG_HOME` — Base directory for `~/.config/sylve/config.yaml`.
4. **Configuration file**: `~/.config/sylve/config.yaml` (`current_context` and `contexts.<name>`).

---

## 4. MCP Tool Architecture (Hybrid Design)

Dumping all 441 Swagger operations as separate tools at startup injects ~75,000 tokens of JSON Schema into the model context and exceeds the tool limits of many MCP clients. `sylve-mcp` solves this with a two-layer hybrid architecture:

### Layer A: Universal OpenAPI Discovery & Execution (All 441 Operations)

Backed by `src/sylve_mcp/openapi.py` and the bundled `docs/swagger.json`:

| Tool | Purpose |
|---|---|
| `sylve_api_search` | Search all 336 paths / 441 operations by keyword, tag (`VM`, `Jail`, `ZFS`, `Network`, `Cluster Backups`, `Directory Services`, `iSCSI`, `Samba`, etc.), or HTTP method. Returns matching operations with summaries and parameter names. |
| `sylve_api_schema` | Inspect the full Swagger operation definition with all `#/definitions/...` `$ref` schemas resolved inline so the model knows exact request body fields, query params, and response types. |
| `sylve_api_call` | Execute any Sylve API endpoint (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`) with `path`, `path_params`, `query_params`, `body`, `upload_file`, `download_path`, `node`, `context`, and `wait`. Automatically handles `X-Current-Hostname` routing, `APIResponse` unwrapping, and task polling. |

### Layer B: Curated Dev/Ops Tools (~30 Cluster-Aware Tools)

High-frequency tools with automatic guest name-to-ID resolution (`web-01` -> `rid`/`ctid` + `node`), multi-node fan-out, human-readable size parsing (`2G`, `20G`), preflight checks, and bounded task waiting (`wait=True`, `wait_timeout=60.0`):

1. **Session & Context (`auth`)**
   - `sylve_whoami` — Active context, server URL, user, node, and JWT expiry status.
   - `sylve_context_list` — List configured contexts in `config.yaml` and show which is active.
2. **Cluster & Nodes (`cluster`, `node`)**
   - `sylve_cluster_status` — Cluster status, Raft leader/members, quorum health, and node summary.
   - `sylve_cluster_resources` — Cluster-wide inventory of all VMs, Jails, and templates across nodes.
   - `sylve_node_list` — All cluster nodes with CPU%, RAM%, Disk%, guest count, and Sylve version.
   - `sylve_node_show` — Combined `/info/node`, `/info/basic`, `/info/cpu`, `/info/ram`, `/info/swap` telemetry.
   - `sylve_node_drain` — Build or execute a maintenance drain plan (HA failovers + live migrations + fallback stop, defaulting to `dry_run=True` for safety).
   - `sylve_node_audit_logs` — Recent audit logs across one or all nodes with parsed request paths.
3. **Bhyve Virtual Machines (`vm`)**
   - `sylve_vm_list` — List VMs across the cluster (summary or `detailed=True` with vCPUs/RAM/autostart).
   - `sylve_vm_show` — Full VM details + live state + QEMU Guest Agent non-loopback IP addresses.
   - `sylve_vm_create` — Provision a VM from scratch or template (auto-allocating `rid` and `vncPort`, parsing `ram`/`disk_size` strings like `2G`/`20G`, validating cloud-init YAML, supporting `dry_run=True`).
   - `sylve_vm_action` — `start`, `stop`, `shutdown`, or `reboot` one or more VMs by name or RID.
   - `sylve_vm_migrate` — Run `/tasks/migration/validate` preflight and migrate a VM (`validate_only` supported).
   - `sylve_vm_delete` — Delete a VM with volume/raw-disk/MAC cleanup flags.
   - `sylve_vm_logs` — Fetch serial console log output for a VM.
   - `sylve_vm_snapshot` — `list`, `create`, `rollback`, or `delete` VM ZFS snapshots.
   - `sylve_vm_template` — `list`, `show`, `capture`, `deploy`, or `delete` VM templates.
4. **FreeBSD Jails (`jail`)**
   - `sylve_jail_list` — List Jails across the cluster with CTID, state, type, cores, and memory.
   - `sylve_jail_show` — Full Jail configuration + live `guest-info`.
   - `sylve_jail_create` — Create a FreeBSD Jail.
   - `sylve_jail_action` — `start`, `stop`, or `restart` one or more Jails by name or CTID.
   - `sylve_jail_migrate` — Validate and migrate a Jail to another cluster node.
   - `sylve_jail_delete` — Delete a Jail (`delete_rootfs`, `delete_macs`).
   - `sylve_jail_logs` — Fetch console logs for a Jail.
   - `sylve_jail_snapshot` — `list`, `create`, `rollback`, or `delete` Jail snapshots.
   - `sylve_jail_bootstrap` — `list`, `create`, or `delete` pkgbase FreeBSD bootstraps on a ZFS pool.
   - `sylve_jail_template` — `list`, `show`, `capture`, `deploy`, or `delete` Jail templates.
5. **ZFS & Downloads (`zfs`, `download`)**
   - `sylve_zfs_pool` — `list`, `status`, `scrub`, `disks_usage` (resolving pool name to GUID automatically).
   - `sylve_zfs_dataset` — `list`, `create_filesystem`, `create_volume`, `edit`, `delete`.
   - `sylve_zfs_snapshot` — `list`, `create`, `rollback`, `delete`, `periodic_list`.
   - `sylve_download` — `list`, `show`, `add` (URL/magnet/path), `delete`.
6. **Networking (`network`)**
   - `sylve_network_overview` — Query `interfaces`, `switches`, `routes`, `dhcp`, `firewall_traffic`, `firewall_nat`, `firewall_counters`, `objects`, or `wireguard` status.
7. **HA Replication, Backups & Tasks (`replication`, `backup`, `task`)**
   - `sylve_replication` — `policies`, `policy_show`, `events`, `event_progress`, `failover`.
   - `sylve_backup` — `targets`, `jobs`, `run_job`, `events`, `event_progress`, `restore`.
   - `sylve_task` — `active`, `recent`, `show`, `wait`, `cancel_migration`.

---

## 5. Dev / Ops Improvements Built Into `sylve-mcp`

1. **Read-Only Guard (`--read-only` / `SYLVE_READ_ONLY=1`)**
   - Enforced directly in `SylveClient.request()`: any non-`GET` request raises `SylveError("Server is running in read-only mode")` before touching the network.
2. **MCP Tool Annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`)**
   - Every tool includes standard MCP annotations so clients (OpenCode, Claude Code, Cursor) can auto-approve safe read queries while prompting before destructive actions (`delete`, `rollback`, `failover`, `drain`).
3. **Production Spec-Drift & Phantom-Route Guard**
   - Detects `200 OK` responses with `Content-Type: text/html` (SvelteKit catch-all on unreleased routes like `/network/interface/l3` or `/tasks/lifecycle/{taskId}` on 0.3.1) and raises a clean `SylveAPIError` instead of returning raw HTML to the model.
   - Includes the `/tasks/lifecycle/active` + `/tasks/lifecycle/recent` fallback in `tasks.py`.
4. **Bounded Async Task Waiting**
   - Lifecycle, migration, replication, and backup actions accept `wait: bool = True` and `wait_timeout: float = 60.0`. If a long-running task exceeds `wait_timeout`, the tool returns the in-progress task record and `taskId` cleanly instead of hanging the MCP transport.
5. **MCP Resources & Ops Prompts**
   - **Resources**:
     - `sylve://cluster/status` — Live cluster & Raft status JSON.
     - `sylve://cluster/resources` — All VMs, Jails, and templates across nodes.
     - `sylve://openapi/tags` — Summary of all 26 Swagger tags and operation counts.
   - **Prompts**:
     - `triage-cluster` — Inspect cluster health, offline nodes, failed tasks, and ZFS pool status.
     - `plan-node-drain` — Run a dry-run maintenance drain plan and report any blocked guests or pinned backup jobs.

---

## 6. Implementation Phases

1. **Phase 1 — Project Scaffold & Spec**
   - Create `pyproject.toml`, `Makefile`, `tox.ini`, `LICENSE`, `MANIFEST.in`, `packaging/freebsd/sysutils/py-sylve-mcp/`, and copy `docs/swagger.json` from `sylve-cli`.
2. **Phase 2 — Core Client, Config & Task Poller**
   - Implement `src/sylve_mcp/config.py`, `src/sylve_mcp/client.py` (with `read_only` mode, HTML response guard, and ZFS pool name-to-GUID resolver), and `src/sylve_mcp/tasks.py` (headless polling with timeout return).
3. **Phase 3 — OpenAPI Engine (`src/sylve_mcp/openapi.py`)**
   - Implement Swagger 2.0 loader, `$ref` resolver, operation search/filter, schema inspector, and optional dynamic per-tag tool generator.
4. **Phase 4 — MCP Protocol & Tools (`src/sylve_mcp/protocol.py`, `src/sylve_mcp/tools.py`, `src/sylve_mcp/main.py`)**
   - Implement JSON-RPC 2.0 `stdio` loop (`initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`).
   - Register all curated Dev/Ops tools + `sylve_api_search`, `sylve_api_schema`, `sylve_api_call`.
5. **Phase 5 — Unit Tests, Live Suite, Man Page & Docs**
   - Write unit tests in `tests/unit/` (including 100% Swagger 336-path / 441-op coverage verification) and live tests in `tests/live/`.
   - Build `docs/man/man1/sylve-mcp.1` and `README.md` with ready-to-copy MCP configuration snippets.
   - Run `make test`, `make lint`, `make type`, and `make man`.