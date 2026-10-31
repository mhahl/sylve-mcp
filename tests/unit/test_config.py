"""Unit tests for configuration loading and environment overrides."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sylve_mcp.config import (
    ContextConfig,
    extract_token_expiry,
    get_default_config_path,
    load_config,
    normalize_server_url,
)


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


def write_config(path: Path, server: str = "https://sylve.example.test", token: str = "") -> Path:
    path.write_text(
        "\n".join(
            [
                "current_context: prod",
                "contexts:",
                "  prod:",
                f"    server: {server}",
                "    username: admin",
                "    auth_type: sylve",
                "    node_id: node-1",
                "    hostname: node1.example.test",
                f"    token: '{token}'",
                "    verify_ssl: true",
                "    default_node: node1.example.test",
                "  lab:",
                "    server: https://sylve-lab.example.test",
                "    username: admin",
                "    auth_type: pam",
                "    token: labtoken",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _clear_sylve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SYLVE_CONFIG",
        "SYLVE_CONTEXT",
        "SYLVE_SERVER",
        "SYLVE_TOKEN",
        "SYLVE_NODE",
        "SYLVE_INSECURE",
        "SYLVE_READ_ONLY",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_config_path_honors_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYLVE_CONFIG", str(tmp_path / "custom.yaml"))
    assert get_default_config_path() == tmp_path / "custom.yaml"

    monkeypatch.delenv("SYLVE_CONFIG")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert get_default_config_path() == tmp_path / "sylve" / "config.yaml"


def test_normalize_server_url() -> None:
    assert normalize_server_url("sylve.example.test/api/") == "https://sylve.example.test"
    assert normalize_server_url("http://host:8181") == "http://host:8181"
    with pytest.raises(ValueError):
        normalize_server_url("   ")


def test_load_config_reads_contexts_and_fields(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.yaml")
    cfg = load_config(path)
    assert cfg.current_context == "prod"
    assert set(cfg.contexts) == {"prod", "lab"}
    assert cfg.contexts["prod"].default_node == "node1.example.test"
    assert cfg.contexts["lab"].auth_type == "pam"


def test_missing_config_file_is_empty(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.contexts == {}


def test_active_context_override(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path / "config.yaml"))
    name, ctx = cfg.get_active_context("lab")
    assert name == "lab"
    assert ctx.server == "https://sylve-lab.example.test"


def test_active_context_falls_back_to_env_credentials() -> None:
    from sylve_mcp.config import SylveConfig

    cfg = SylveConfig()
    import os

    os.environ["SYLVE_SERVER"] = "sylve-env.test"
    os.environ["SYLVE_TOKEN"] = make_jwt(datetime.now(timezone.utc) + timedelta(hours=1))
    name, ctx = cfg.get_active_context()
    assert name == "env"
    assert ctx.server == "https://sylve-env.test"


def test_active_context_without_any_login_raises() -> None:
    from sylve_mcp.config import SylveConfig

    with pytest.raises(RuntimeError, match="Not logged in"):
        SylveConfig().get_active_context()


def test_env_overrides_apply_to_saved_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(write_config(tmp_path / "config.yaml", token="filetoken"))
    monkeypatch.setenv("SYLVE_TOKEN", "envtoken")
    monkeypatch.setenv("SYLVE_NODE", "node9")
    monkeypatch.setenv("SYLVE_INSECURE", "yes")
    _, ctx = cfg.get_active_context("prod")
    assert ctx.token == "envtoken"
    assert ctx.default_node == "node9"
    assert ctx.verify_ssl is False


def test_token_expiry_and_remaining() -> None:
    token = make_jwt(datetime.now(timezone.utc) + timedelta(hours=2))
    ctx = ContextConfig(server="https://x.test", token=token)
    assert extract_token_expiry(token) is not None
    assert ctx.is_token_expired() is False
    assert "h" in ctx.format_remaining()

    expired = ContextConfig(
        server="https://x.test",
        token=make_jwt(datetime.now(timezone.utc) - timedelta(minutes=1)),
    )
    assert expired.is_token_expired() is True
    assert expired.format_remaining() == "expired"
