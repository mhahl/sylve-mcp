# SYLVE-MCP(1)

## NAME

`sylve-mcp` — Model Context Protocol server for Sylve FreeBSD clusters, Bhyve VMs, Jails, ZFS, Networking, and Backups

## SYNOPSIS

```sh
sylve-mcp [--config FILE] [--context NAME] [-n NODE] [-k] [--timeout SECONDS]
          [--read-only] [--expose-tags TAG1,TAG2] [--expose-all] [--swagger FILE] [-V] [-h]
```

## DESCRIPTION

`sylve-mcp` exposes the Sylve REST API to agents and models over the Model
Context Protocol (MCP) `stdio` transport. It is a companion to
[`sylve-cli`](https://github.com/mhahl/sylve-cli) and
[`sylve-ansible`](https://github.com/mhahl/sylve-ansible); all three programs
share the same configuration file and `SYLVE_*` environment variables.

The server speaks newline-delimited JSON-RPC 2.0 on standard input and output
and implements `initialize`, `ping`, `tools/list`, `tools/call`,
`resources/list`, `resources/read`, `prompts/list`, and `prompts/get`. It uses
only the standard library for the protocol — the runtime dependencies are
`httpx` and `PyYAML` (matching the `sylve-cli` stack).

Every endpoint of the bundled [OpenAPI spec](docs/swagger.json) (Sylve 0.3.1:
336 paths, 441 operations) is reachable. The server registers 35 curated,
cluster-aware tools plus three universal Swagger tools that cover the rest.

## INSTALLATION

Requires Python 3.10 or newer.

From a local checkout with `pip`:

```sh
pip install .
```

From the FreeBSD port:

```sh
cd packaging/freebsd/sysutils/py-sylve-mcp
make package
```

## OPTIONS

Operate cluster-wide by default; override per call by passing `node` or
`context` as tool arguments.

- `--config FILE`
  Path to the configuration file (default: `~/.config/sylve/config.yaml`).
- `--context NAME`
  Named login context to use by default (see `sylve_context_list`).
- `-n, --node NODE`
  Default target cluster node hostname or UUID.
- `-k, --insecure`
  Disable TLS certificate verification.
- `--timeout SECONDS`
  HTTP request timeout in seconds (default: `30.0`).
- `--read-only`
  Refuse every non-`GET` API request before it leaves the process. Safe for
  unattended production triage.
- `--expose-tags TAG1,TAG2`
  Also register one MCP tool per Swagger operation in the given comma-separated
  tags (e.g. `VM,Jail,ZFS`).
- `--expose-all`
  Register one MCP tool for every one of the 441 Swagger operations.
- `--swagger FILE`
  Override the bundled `swagger.json` path.
- `-V, --version`
  Print the version and exit.
- `-h, --help`
  Print usage information and exit.

## COMMANDS

### Curated cluster-aware tools (35)

| Group | Tools |
|---|---|
| Session | `sylve_whoami`, `sylve_context_list` |
| Cluster / nodes | `sylve_cluster_status`, `sylve_cluster_resources`, `sylve_node_list`, `sylve_node_show`, `sylve_node_audit_logs`, `sylve_node_drain` |
| VMs | `sylve_vm_list`, `sylve_vm_show`, `sylve_vm_create`, `sylve_vm_action`, `sylve_vm_migrate`, `sylve_vm_delete`, `sylve_vm_logs`, `sylve_vm_snapshot`, `sylve_vm_template` |
| Jails | `sylve_jail_list`, `sylve_jail_show`, `sylve_jail_create`, `sylve_jail_action`, `sylve_jail_migrate`, `sylve_jail_delete`, `sylve_jail_logs`, `sylve_jail_snapshot`, `sylve_jail_bootstrap`, `sylve_jail_template` |
| Storage | `sylve_zfs_pool`, `sylve_zfs_dataset`, `sylve_zfs_snapshot`, `sylve_download` |
| Network | `sylve_network_overview` |
| HA / backup / tasks | `sylve_replication`, `sylve_backup`, `sylve_task` |

Curated tools resolve VM names, jail names, ZFS pool names, and dataset names
to IDs automatically, accept human-readable sizes (`2G`, `20G`), expose live
guest-agent IP addresses, and poll asynchronous tasks to `wait_timeout`
(default `60s`) — returning the in-progress task record instead of hanging.

### Universal Swagger tools

| Tool | Purpose |
|---|---|
| `sylve_api_search` | Search all operations by keyword, Swagger tag, or HTTP method. |
| `sylve_api_schema` | Full request/response schema for one operation, `$ref`s resolved. |
| `sylve_api_call` | Execute any documented operation with `path_params`, `query_params`, `body`, `upload_file`, or `download_path`. |

For an endpoint without a dedicated tool, follow the workflow:
`sylve_api_search` → `sylve_api_schema` → `sylve_api_call`.

### Resources and prompts

- Resources: `sylve://cluster/status`, `sylve://cluster/resources`, `sylve://openapi/tags`
- Prompts: `triage-cluster`, `plan-node-drain`

## HOW TO USE

### 1. Authenticate

`sylve-mcp` does not log in. Reuse an existing `sylve` session, or provide
credentials via the environment.

Save a context (writes `~/.config/sylve/config.yaml`, mode `0600`):

```sh
sylve login https://sylve.example.test:8181
```

Or run purely from the environment in CI/containers:

```sh
export SYLVE_SERVER=https://sylve.example.test:8181
export SYLVE_TOKEN=eyJhbGciOi...
export SYLVE_NODE=node1.example.test   # optional default node
```

### 2. Start the server

```sh
sylve-mcp                       # uses the active context from config.yaml
sylve-mcp --context prod        # a specific saved context
sylve-mcp --read-only           # read-only: refuse all non-GET requests
sylve-mcp --expose-tags VM,Jail # also register one tool per VM/Jail operation
```

The server reads JSON-RPC messages from stdin and writes responses to stdout,
so it is normally launched by an MCP client rather than a shell.

### 3. Register with an MCP client

**OpenCode** (`opencode.json`):

```json
{
  "mcp": {
    "sylve": { "type": "stdio", "command": ["sylve-mcp"], "enabled": true }
  }
}
```

**Claude Desktop / Claude Code** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sylve": { "command": "sylve-mcp", "args": [] }
  }
}
```

**Cursor** (`.cursor/mcp.json`) uses the same `mcpServers` shape as Claude.

Add `"args": ["--read-only"]` when the client may act unattended.

### 4. Drive it from the agent

Read-only and destructive operations are distinguished for the client via MCP
tool annotations (`readOnlyHint` / `destructiveHint`), so safe queries run
automatically and state changes prompt for confirmation:

```text
"List all VMs and their states"            -> sylve_vm_list
"Show vm 101 with its IPs"                  -> sylve_vm_show
"Create a 2G/20G VM web-01 on zroot"        -> sylve_vm_create
"Drain node1 for maintenance (dry run)"     -> sylve_node_drain (dry_run=true)
"Restore the latest backup job 12 snapshot" -> sylve_backup
"Wait on lifecycle task 42"                 -> sylve_task action=wait
"Find the login-config endpoint"            -> sylve_api_search query=login
```

## ENVIRONMENT

- `SYLVE_CONFIG` — override configuration file path.
- `SYLVE_CONTEXT` — override active context name.
- `SYLVE_SERVER` — override server URL without a saved context.
- `SYLVE_TOKEN` — override bearer token without a saved context.
- `SYLVE_NODE` — default target cluster node.
- `SYLVE_INSECURE` — set to `1`, `true`, or `yes` to skip TLS verification.
- `SYLVE_READ_ONLY` — set to `1`, `true`, or `yes` to enforce read-only mode.
- `SYLVE_SWAGGER` — override the bundled `swagger.json` path.
- `XDG_CONFIG_HOME` — base directory for the default configuration file.

## FILES

- `~/.config/sylve/config.yaml`
  Default configuration file containing saved login contexts and bearer tokens
  (mode 0600), created by `sylve login`.
- `docs/swagger.json`
  Vendored Sylve OpenAPI 2.0 specification (also packaged as
  `sylve_mcp/swagger.json`). Regenerate from the upstream Sylve repository.
- `docs/man/man1/sylve-mcp.1`
  Roff manual page, rendered from `docs/man/src/sylve-mcp.1.in` with
  `make man`.

## EXIT STATUS

- `0` — clean shutdown at end of input (EOF), or `--help` / `--version`.
- `1` — startup error (invalid spec path, bad configuration).

Tool-call failures are returned to the MCP client as `isError: true` in the
JSON-RPC response and do not terminate the server.

## EXAMPLES

Provision a VM, then attach its console logs in a later message:

```text
sylve_vm_create(name="web-01", memory="2G", disk_size="20G",
                pool="zroot", cloud_init=true, user_data="#cloud-config ...",
                start=true)
sylve_vm_logs(vm="web-01")
```

Triage a degraded cluster with the built-in prompt:

```text
/prompt triage-cluster
```

## SEE ALSO

- [sylve-cli](https://github.com/mhahl/sylve-cli) — the `sylve(1)` command line, `sylve-config(5)`
- [sylve-ansible](https://github.com/mhahl/sylve-ansible) — the `sylve.cloud` Ansible collection
- [TESTING.md](TESTING.md) — unit and live test suites
- [PLAN.md](PLAN.md) — design and implementation notes

`sylve-mcp` is an independent tool and is not affiliated with or endorsed by
the Sylve project. It is licensed under the BSD-2-Clause license.