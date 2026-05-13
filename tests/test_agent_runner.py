"""Tests for the agent runner — role resolution + prompt assembly."""

from __future__ import annotations

from pathlib import Path

from workestrator.agent_runner import AgentRunner
from workestrator.config import AgentConfig


def _runner(roles_dir: Path) -> AgentRunner:
    return AgentRunner(
        config=AgentConfig(),
        roles_dir=roles_dir,
        pearscarf_url="http://localhost:8090/sse",
    )


def test_resolve_role_prefers_owner(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    assert r.resolve_role_key({"owner": "hex", "owner_role": "head-eng"}) == "hex"


def test_resolve_role_falls_back_to_owner_role(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    assert r.resolve_role_key({"owner": None, "owner_role": "head-eng"}) == "head-eng"


def test_resolve_role_none_when_neither(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    assert r.resolve_role_key({}) is None


def test_load_role_prompt_reads_markdown(tmp_path: Path) -> None:
    (tmp_path / "hex").mkdir()
    (tmp_path / "hex" / "prompt.md").write_text("You are hex.")
    r = _runner(tmp_path)
    assert r.load_role_prompt("hex") == "You are hex."


def test_load_role_prompt_missing_returns_none(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    assert r.load_role_prompt("ghost") is None


def test_build_user_message_wraps_body(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    intent = {"intent_id": "intent_abc", "body": "Ship the launch demo."}
    msg = r.build_user_message(intent)
    assert "intent_abc" in msg
    assert "Ship the launch demo." in msg
    assert "submit_record" in msg
    assert "set_intent_status" in msg


def test_build_user_message_accepts_id_field(tmp_path: Path) -> None:
    """Storage layer uses `id`, MCP layer uses `intent_id`. Both work."""
    r = _runner(tmp_path)
    msg = r.build_user_message({"id": "intent_z", "body": "do x"})
    assert "intent_z" in msg
