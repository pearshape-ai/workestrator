"""Tests for the agent runner — role resolution + prompt assembly."""

from __future__ import annotations

import json
from pathlib import Path

from workestrator.agent_runner import (
    AgentRunner,
    _summarize_tool_input,
    _trunc,
)
from workestrator.config import AgentConfig
from workestrator.events import EventEmitter


def _runner(roles_dir: Path, events: EventEmitter | None = None) -> AgentRunner:
    return AgentRunner(
        config=AgentConfig(),
        roles_dir=roles_dir,
        pearscarf_url="http://localhost:8090/sse",
        events=events,
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


# ---- per-message event emission ----


def _events_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_emit_message_events_emits_agent_text_for_text_block(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events = EventEmitter(events_path)
    r = _runner(tmp_path, events=events)
    msg_dict = {
        "type": "AssistantMessage",
        "content": [{"type": "text", "text": "I'm reading the CLI now."}],
    }
    r._emit_message_events(msg_dict, "intent_1", "eng")
    rows = _events_lines(events_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "agent_text"
    assert rows[0]["intent_id"] == "intent_1"
    assert rows[0]["role"] == "eng"
    assert rows[0]["text"] == "I'm reading the CLI now."


def test_emit_message_events_emits_agent_tool_use_for_tool_block(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events = EventEmitter(events_path)
    r = _runner(tmp_path, events=events)
    msg_dict = {
        "type": "AssistantMessage",
        "content": [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "cd pearscarf && cat pearscarf/cli.py"},
            }
        ],
    }
    r._emit_message_events(msg_dict, "intent_2", "eng")
    rows = _events_lines(events_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "agent_tool_use"
    assert rows[0]["tool"] == "Bash"
    assert "cd pearscarf" in rows[0]["summary"]


def test_emit_message_events_emits_multiple_blocks_in_order(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events = EventEmitter(events_path)
    r = _runner(tmp_path, events=events)
    msg_dict = {
        "type": "AssistantMessage",
        "content": [
            {"type": "text", "text": "Reading the file now."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x.py"}},
        ],
    }
    r._emit_message_events(msg_dict, "intent_3", "eng")
    rows = _events_lines(events_path)
    assert [r["event"] for r in rows] == ["agent_text", "agent_tool_use"]
    assert rows[1]["tool"] == "Read"
    assert rows[1]["summary"] == "/tmp/x.py"


def test_emit_message_events_skips_non_assistant_messages(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events = EventEmitter(events_path)
    r = _runner(tmp_path, events=events)
    r._emit_message_events(
        {"type": "UserMessage", "content": "tool result"},
        "intent_4",
        "eng",
    )
    r._emit_message_events(
        {"type": "ResultMessage", "result": "done"},
        "intent_4",
        "eng",
    )
    assert _events_lines(events_path) == []


def test_emit_message_events_no_op_when_events_unset(tmp_path: Path) -> None:
    """AgentRunner constructed without an events emitter shouldn't crash."""
    r = _runner(tmp_path, events=None)
    r._emit_message_events(
        {"type": "AssistantMessage", "content": [{"type": "text", "text": "hi"}]},
        "intent_5",
        "eng",
    )


def test_trunc_under_limit_returns_input() -> None:
    assert _trunc("hello") == "hello"


def test_trunc_over_limit_appends_ellipsis() -> None:
    s = "x" * 300
    out = _trunc(s, 100)
    assert len(out) <= 101  # 100 chars + 1 ellipsis
    assert out.endswith("…")


def test_summarize_tool_input_bash_uses_command_field() -> None:
    assert _summarize_tool_input("Bash", {"command": "ls -la"}) == "ls -la"


def test_summarize_tool_input_edit_uses_file_path() -> None:
    assert (
        _summarize_tool_input("Edit", {"file_path": "/tmp/x.py", "old_string": "a"})
        == "/tmp/x.py"
    )


def test_summarize_tool_input_mcp_dumps_json() -> None:
    out = _summarize_tool_input(
        "mcp__pearscarf__query_facts", {"subject": "PearScarf", "limit": 10}
    )
    assert "PearScarf" in out
    assert "subject" in out
