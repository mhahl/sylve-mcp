# TESTING

## Unit tests (offline)

```sh
make test
# or
python -m pytest tests/unit
```

Everything runs against mocked HTTP transports — no Sylve server is required.

| File | What it covers |
|---|---|
| `tests/unit/test_config.py` | Context loading, env overrides, JWT expiry, URL normalization. |
| `tests/unit/test_client.py` | Auth (missing/expired/401), node headers, read-only guard, HTML catch-all guard, pad error surfaces, guest/pool resolution. |
| `tests/unit/test_openapi.py` | Vendored spec is the advertised 336 paths / 441 operations, every operation is reachable, `$ref` expansion, search/tag filters, dynamic tool generation. |
| `tests/unit/test_protocol.py` | JSON-RPC framing, initialize capabilities, tools/resources/prompts, error codes, notifications, parse errors. |
| `tests/unit/test_tools.py` | Every curated tool plus the universal API tools, tool annotations, and server wiring. |

## Live tests (read-only)

```sh
export SYLVE_LIVE=1
export SYLVE_SERVER=https://sylve.example.test:8181
export SYLVE_TOKEN=eyJhbGciOi...
export SYLVE_NODE=node1.example.test   # optional
export SYLVE_INSECURE=1                # optional
make live-test
```

The live suite exercises every read-only tool over the real JSON-RPC layer and
never sends a mutating request. Tools that are unavailable on the target build
(missing guests, unreleased routes, permission errors) are reported as skips.

## Lint, types, man page

```sh
make lint        # ruff check + ruff format --check + pylint --errors-only
make type        # mypy
make man         # render docs/man/man1/sylve-mcp.1
make tox         # full matrix (py310-313, lint, type, live, man)
```

## Manual protocol smoke test

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
| sylve-mcp | head -c 400
```
