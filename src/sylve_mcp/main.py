"""Console entry point for the sylve-mcp server.

``run`` parses a small set of global options then serves MCP over stdio, so it
works as the ``command`` in an MCP client configuration with no extra flags.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sylve_mcp import __version__
from sylve_mcp.openapi import OpenAPI
from sylve_mcp.protocol import MCPServer
from sylve_mcp.tools import (
    MCPSettings,
    register_dynamic_tools,
    register_openapi_tools,
    register_resources_and_prompts,
    register_tools,
)

_TRUTHY = {"1", "true", "yes", "on"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sylve-mcp",
        description="Model Context Protocol (MCP) server for Sylve FreeBSD clusters.",
        add_help=False,
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="Path to the Sylve configuration file."
    )
    parser.add_argument("--context", default=None, help="Default named login context.")
    parser.add_argument(
        "-n", "--node", default=None, help="Default target cluster node hostname or UUID."
    )
    parser.add_argument(
        "-k", "--insecure", action="store_true", help="Disable TLS certificate verification."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds (default: 30.0).",
    )
    parser.add_argument("--read-only", action="store_true", help="Refuse all non-GET API requests.")
    parser.add_argument(
        "--expose-tags",
        default=None,
        help="Register one tool per Swagger operation for these comma-separated tags.",
    )
    parser.add_argument(
        "--expose-all", action="store_true", help="Register one tool for every Swagger operation."
    )
    parser.add_argument(
        "--swagger", type=Path, default=None, help="Override the bundled swagger.json path."
    )
    parser.add_argument("-V", "--version", action="version", version=f"sylve-mcp {__version__}")
    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit.")
    return parser


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def build_server(settings: MCPSettings, args: argparse.Namespace) -> MCPServer:
    """Build a fully registered MCP server from parsed settings."""
    spec = OpenAPI(settings.swagger)
    server = MCPServer(
        {
            "name": "sylve-mcp",
            "version": __version__,
            "title": "Sylve MCP",
        }
    )
    register_tools(server, settings)
    register_openapi_tools(server, settings, spec)
    register_resources_and_prompts(server, settings, spec)

    tags: set[str] | None = None
    if args.expose_tags:
        tags = {t.strip().lower() for t in args.expose_tags.split(",") if t.strip()}
    if args.expose_all:
        tags = None
        register_dynamic_tools(server, settings, spec, None)
    elif tags:
        register_dynamic_tools(server, settings, spec, tags)
    return server


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = MCPSettings(
        config_path=args.config,
        context=args.context,
        node=args.node,
        insecure=args.insecure or _env_truthy("SYLVE_INSECURE"),
        timeout=args.timeout,
        read_only=args.read_only or _env_truthy("SYLVE_READ_ONLY"),
        swagger=args.swagger
        or (Path(os.environ["SYLVE_SWAGGER"]) if os.environ.get("SYLVE_SWAGGER") else None),
    )
    server = build_server(settings, args)
    return server.run()


def run() -> None:
    """Console-script entry point."""
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    run()
