"""Typed YAML config loader.

Reads `workestrator.yaml` (or whatever path the CLI is pointed at), expands
`${VAR}` references from the environment, applies defaults, and returns a
typed `Config` dataclass tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PearscarfConfig:
    mcp_url: str
    api_key: str | None = None


@dataclass
class OrchestratorConfig:
    poll_interval_seconds: int = 30
    max_concurrent_agents: int = 4


@dataclass
class RolesConfig:
    dir: Path


@dataclass
class AgentConfig:
    model: str = "claude-sonnet-4-5"
    max_turns: int = 50


@dataclass
class WorkspaceConfig:
    dir: Path


@dataclass
class Config:
    pearscarf: PearscarfConfig
    orchestrator: OrchestratorConfig
    roles: RolesConfig
    agent: AgentConfig
    workspace: WorkspaceConfig


def _expand(value: Any) -> Any:
    """Recursively expand `${VAR}` / `$VAR` references in string values."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _resolve(base: Path, raw_path: str) -> Path:
    """Resolve a config-relative path against the config file's directory."""
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def load(config_path: Path) -> Config:
    """Load and validate a workestrator YAML config."""
    config_path = Path(config_path).resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"workestrator config not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text()) or {}
    raw = _expand(raw)
    base_dir = config_path.parent

    pearscarf_raw = raw.get("pearscarf") or {}
    orch_raw = raw.get("orchestrator") or {}
    roles_raw = raw.get("roles") or {}
    agent_raw = raw.get("agent") or {}
    workspace_raw = raw.get("workspace") or {}

    if not pearscarf_raw.get("mcp_url"):
        raise ValueError("pearscarf.mcp_url is required")

    api_key = pearscarf_raw.get("api_key")
    if api_key in ("", None, "${PEARSCARF_API_KEY}"):
        api_key = None

    return Config(
        pearscarf=PearscarfConfig(
            mcp_url=pearscarf_raw["mcp_url"],
            api_key=api_key,
        ),
        orchestrator=OrchestratorConfig(
            poll_interval_seconds=int(orch_raw.get("poll_interval_seconds", 30)),
            max_concurrent_agents=int(orch_raw.get("max_concurrent_agents", 4)),
        ),
        roles=RolesConfig(
            dir=_resolve(base_dir, roles_raw.get("dir", "./roles")),
        ),
        agent=AgentConfig(
            model=agent_raw.get("model", "claude-sonnet-4-5"),
            max_turns=int(agent_raw.get("max_turns", 50)),
        ),
        workspace=WorkspaceConfig(
            dir=_resolve(base_dir, workspace_raw.get("dir", "./.workestrator/workspaces")),
        ),
    )
