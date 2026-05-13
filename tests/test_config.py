"""Tests for the YAML config loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from workestrator.config import load


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "workestrator.yaml"
    path.write_text(body)
    return path


def test_load_defaults(tmp_path: Path) -> None:
    cfg = load(
        _write(
            tmp_path,
            """
pearscarf:
  mcp_url: http://localhost:8090/sse
roles:
  dir: ./roles
""",
        )
    )
    assert cfg.pearscarf.mcp_url == "http://localhost:8090/sse"
    assert cfg.pearscarf.api_key is None
    assert cfg.orchestrator.poll_interval_seconds == 30
    assert cfg.orchestrator.max_concurrent_agents == 4
    assert cfg.agent.model == "claude-sonnet-4-5"
    assert cfg.agent.max_turns == 50


def test_load_overrides(tmp_path: Path) -> None:
    cfg = load(
        _write(
            tmp_path,
            """
pearscarf:
  mcp_url: http://localhost:9090/sse
  api_key: secret-token
orchestrator:
  poll_interval_seconds: 5
  max_concurrent_agents: 8
roles:
  dir: ./my_roles
agent:
  model: claude-opus-4-7
  max_turns: 100
workspace:
  dir: /tmp/ws
""",
        )
    )
    assert cfg.pearscarf.api_key == "secret-token"
    assert cfg.orchestrator.poll_interval_seconds == 5
    assert cfg.orchestrator.max_concurrent_agents == 8
    assert cfg.agent.model == "claude-opus-4-7"
    assert cfg.agent.max_turns == 100
    assert cfg.workspace.dir == Path("/tmp/ws")


def test_relative_paths_resolve_against_config(tmp_path: Path) -> None:
    cfg = load(
        _write(
            tmp_path,
            """
pearscarf:
  mcp_url: http://x
roles:
  dir: ./team
workspace:
  dir: ./.ws
""",
        )
    )
    assert cfg.roles.dir == (tmp_path / "team").resolve()
    assert cfg.workspace.dir == (tmp_path / ".ws").resolve()


def test_env_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TEST_URL", "http://expanded:1234/sse")
    cfg = load(
        _write(
            tmp_path,
            """
pearscarf:
  mcp_url: ${MY_TEST_URL}
roles:
  dir: ./r
""",
        )
    )
    assert cfg.pearscarf.mcp_url == "http://expanded:1234/sse"


def test_missing_mcp_url_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
pearscarf: {}
roles:
  dir: ./r
""",
    )
    with pytest.raises(ValueError, match="mcp_url"):
        load(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.yaml")


def test_unset_api_key_placeholder_becomes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEARSCARF_API_KEY", raising=False)
    cfg = load(
        _write(
            tmp_path,
            """
pearscarf:
  mcp_url: http://x
  api_key: ${PEARSCARF_API_KEY}
roles:
  dir: ./r
""",
        )
    )
    assert cfg.pearscarf.api_key is None
