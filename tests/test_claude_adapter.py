"""Unit tests for the Claude adapter — pure-logic helpers.

Subprocess-level integration (`claude --print` end-to-end) is exercised
out-of-band; this file tests only the translation + command-builder logic.
"""

from __future__ import annotations

import json

from workestrator.adapters.claude import ClaudeAdapter, _stream_event_to_message_dict
from workestrator.config import AgentConfig


def test_translate_assistant_lifts_message_content_to_top_level() -> None:
    """stream-json assistant events nest content under `.message.content`;
    the core's event emitter expects it at the top level."""
    ev = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ],
        },
        "session_id": "s1",
    }
    out = _stream_event_to_message_dict(ev)
    assert out["type"] == "assistant"
    assert out["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ]


def test_translate_passes_non_assistant_events_through_unchanged() -> None:
    """`system`, `user` (tool_result), `result`, `rate_limit_event` go through as-is."""
    for ev in [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
        {"type": "result", "subtype": "success", "total_cost_usd": 0.42},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    ]:
        assert _stream_event_to_message_dict(ev) == ev


def test_translate_missing_message_content_returns_empty_list() -> None:
    """Defensive: a malformed assistant event without `message.content` shouldn't crash."""
    out = _stream_event_to_message_dict({"type": "assistant"})
    assert out == {"type": "assistant", "content": []}


def _adapter(model: str = "claude-sonnet-4-6") -> ClaudeAdapter:
    return ClaudeAdapter(
        config=AgentConfig(model=model),
        pearscarf_url="http://localhost:8090/sse",
        pearscarf_api_key="testkey",
    )


def test_build_mcp_config_includes_pearscarf_with_auth() -> None:
    """The MCP config JSON the adapter passes via `--mcp-config` must carry
    the pearscarf SSE URL + Bearer auth header."""
    cfg = json.loads(_adapter()._build_mcp_config())
    assert cfg == {
        "mcpServers": {
            "pearscarf": {
                "type": "sse",
                "url": "http://localhost:8090/sse",
                "headers": {"Authorization": "Bearer testkey"},
            }
        }
    }


def test_build_mcp_config_skips_headers_when_no_auth() -> None:
    a = ClaudeAdapter(
        config=AgentConfig(),
        pearscarf_url="http://localhost:8090/sse",
        pearscarf_api_key=None,
    )
    cfg = json.loads(a._build_mcp_config())
    assert "headers" not in cfg["mcpServers"]["pearscarf"]


def test_build_command_has_required_print_flags() -> None:
    """Every claude subprocess we spawn needs --print + stream-json + verbose +
    bypassPermissions + no-session-persistence; without these the agent runtime
    semantics break."""
    cmd = _adapter()._build_command(
        runtime_config={},
        system_prompt="SYS",
        user_message="USER",
    )
    assert "--print" in cmd
    assert "stream-json" in cmd
    assert "--verbose" in cmd
    assert "bypassPermissions" in cmd
    assert "--no-session-persistence" in cmd
    # User message is the last positional arg.
    assert cmd[-1] == "USER"
    # System prompt threads through the --system-prompt flag.
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"


def test_build_command_chrome_flag_only_when_required() -> None:
    cmd_no = _adapter()._build_command(
        runtime_config={"chrome_required": False},
        system_prompt="",
        user_message="",
    )
    assert "--chrome" not in cmd_no
    cmd_yes = _adapter()._build_command(
        runtime_config={"chrome_required": True},
        system_prompt="",
        user_message="",
    )
    assert "--chrome" in cmd_yes


def test_build_command_runtime_config_model_overrides_config_default() -> None:
    cmd = _adapter("claude-haiku-4-5")._build_command(
        runtime_config={"model": "claude-opus-4-7"},
        system_prompt="",
        user_message="",
    )
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"


def test_build_command_falls_back_to_config_model_when_runtime_config_silent() -> None:
    cmd = _adapter("claude-sonnet-4-6")._build_command(
        runtime_config={},
        system_prompt="",
        user_message="",
    )
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_build_command_includes_max_budget_when_set() -> None:
    cmd = _adapter()._build_command(
        runtime_config={"max_budget_usd": 5.0},
        system_prompt="",
        user_message="",
    )
    assert "--max-budget-usd" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "5.0"


def test_build_command_omits_max_budget_when_unset() -> None:
    cmd = _adapter()._build_command(
        runtime_config={},
        system_prompt="",
        user_message="",
    )
    assert "--max-budget-usd" not in cmd
