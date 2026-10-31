"""Configuration and session token management for sylve-mcp.

This mirrors the sylve-cli configuration contract exactly so the MCP server,
the CLI, and the Ansible collection all authenticate against the same
``~/.config/sylve/config.yaml`` contexts and ``SYLVE_*`` environment variables.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def get_default_config_path() -> Path:
    """Return the default configuration file path.

    Returns:
        Path: ``$SYLVE_CONFIG``, ``$XDG_CONFIG_HOME/sylve/config.yaml``, or
        ``~/.config/sylve/config.yaml``.
    """
    env_path = os.environ.get("SYLVE_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "sylve" / "config.yaml"


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying its signature."""
    if not token or "." not in token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    padding = "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(parts[1] + padding)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def extract_token_expiry(token: str) -> str | None:
    """Extract the JWT ``exp`` claim as an ISO-8601 UTC timestamp."""
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return None


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not value:
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def normalize_server_url(server: str) -> str:
    """Normalize a Sylve server URL (add scheme, strip ``/api`` and trailing slash)."""
    s = server.strip()
    if not s:
        raise ValueError("Server URL cannot be empty")
    if not s.startswith(("http://", "https://")):
        s = f"https://{s}"
    s = s.rstrip("/")
    s = s.removesuffix("/api")
    return s


@dataclass
class ContextConfig:
    """Represents a single Sylve server/cluster login context."""

    server: str
    username: str = ""
    auth_type: str = "sylve"
    node_id: str = ""
    hostname: str = ""
    token: str = ""
    expires_at: str | None = None
    verify_ssl: bool = True
    ca_cert: str | None = None
    default_node: str | None = None

    def get_expiry_datetime(self) -> datetime | None:
        dt = parse_iso_datetime(self.expires_at)
        if dt is not None:
            return dt
        if self.token:
            return parse_iso_datetime(extract_token_expiry(self.token))
        return None

    def is_token_expired(self, skew_seconds: int = 0) -> bool:
        if not self.token:
            return True
        exp = self.get_expiry_datetime()
        if exp is None:
            return False
        now = datetime.now(timezone.utc)
        return (exp.timestamp() - now.timestamp()) <= skew_seconds

    def remaining_seconds(self) -> float | None:
        exp = self.get_expiry_datetime()
        if exp is None:
            return None
        now = datetime.now(timezone.utc)
        return exp.timestamp() - now.timestamp()

    def format_remaining(self) -> str:
        rem = self.remaining_seconds()
        if rem is None:
            return "unknown"
        if rem <= 0:
            return "expired"
        total_sec = int(rem)
        days, r = divmod(total_sec, 86400)
        hours, r = divmod(r, 3600)
        minutes, seconds = divmod(r, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)


@dataclass
class SylveConfig:
    """Top-level configuration holding named contexts and the active context."""

    current_context: str = ""
    contexts: dict[str, ContextConfig] = field(default_factory=dict)

    def get_active_context(self, context_override: str | None = None) -> tuple[str, ContextConfig]:
        """Resolve the active context with environment-credential fallback.

        Follows the same precedence as sylve-cli: explicit override, then
        ``$SYLVE_CONTEXT``, then the saved ``current_context``. A pure
        environment deployment (``SYLVE_SERVER`` + ``SYLVE_TOKEN``) also works.
        """
        ctx_name = context_override or os.environ.get("SYLVE_CONTEXT") or self.current_context
        if not ctx_name or ctx_name not in self.contexts:
            env_server = os.environ.get("SYLVE_SERVER")
            env_token = os.environ.get("SYLVE_TOKEN")
            if env_server and env_token:
                verify_ssl = os.environ.get("SYLVE_INSECURE", "").lower() not in (
                    "1",
                    "true",
                    "yes",
                )
                return "env", ContextConfig(
                    server=normalize_server_url(env_server),
                    token=env_token,
                    expires_at=extract_token_expiry(env_token),
                    verify_ssl=verify_ssl,
                    default_node=os.environ.get("SYLVE_NODE"),
                )
            if not self.contexts:
                raise RuntimeError(
                    "Not logged in to any Sylve server. Run 'sylve login <server-url>' first."
                )
            if ctx_name and ctx_name not in self.contexts:
                raise RuntimeError(
                    f"Context '{ctx_name}' not found in configuration. "
                    f"Available contexts: {', '.join(sorted(self.contexts.keys()))}"
                )
            ctx_name = next(iter(self.contexts.keys()))

        ctx = self.contexts[ctx_name]
        env_server = os.environ.get("SYLVE_SERVER")
        env_token = os.environ.get("SYLVE_TOKEN")
        env_node = os.environ.get("SYLVE_NODE")
        env_insecure = os.environ.get("SYLVE_INSECURE")

        resolved = ContextConfig(
            server=normalize_server_url(env_server) if env_server else ctx.server,
            username=ctx.username,
            auth_type=ctx.auth_type,
            node_id=ctx.node_id,
            hostname=ctx.hostname,
            token=env_token if env_token else ctx.token,
            expires_at=extract_token_expiry(env_token) if env_token else ctx.expires_at,
            verify_ssl=(
                False
                if (env_insecure and env_insecure.lower() in ("1", "true", "yes"))
                else ctx.verify_ssl
            ),
            ca_cert=ctx.ca_cert,
            default_node=env_node if env_node else ctx.default_node,
        )
        return ctx_name, resolved


def load_config(path: Path | None = None) -> SylveConfig:
    """Load configuration from *path* (default: :func:`get_default_config_path`)."""
    cfg_path = path or get_default_config_path()
    if not cfg_path.exists():
        return SylveConfig()
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise RuntimeError(f"Failed to parse configuration file {cfg_path}: {exc}") from exc

    if not isinstance(raw, dict):
        return SylveConfig()

    current_context = str(raw.get("current_context") or "")
    raw_contexts = raw.get("contexts") or {}
    contexts: dict[str, ContextConfig] = {}

    if isinstance(raw_contexts, dict):
        for name, cdata in raw_contexts.items():
            if not isinstance(cdata, dict) or not cdata.get("server"):
                continue
            contexts[str(name)] = ContextConfig(
                server=normalize_server_url(str(cdata.get("server", ""))),
                username=str(cdata.get("username") or ""),
                auth_type=str(cdata.get("auth_type") or "sylve"),
                node_id=str(cdata.get("node_id") or ""),
                hostname=str(cdata.get("hostname") or ""),
                token=str(cdata.get("token") or ""),
                expires_at=cdata.get("expires_at"),
                verify_ssl=bool(cdata.get("verify_ssl", True)),
                ca_cert=cdata.get("ca_cert"),
                default_node=cdata.get("default_node"),
            )

    return SylveConfig(current_context=current_context, contexts=contexts)
