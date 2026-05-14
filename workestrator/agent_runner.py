"""Spawn one Claude agent session per intent.

The agent's system prompt is the role's `prompt.md`; its user message is
the intent body. Pearscarf is attached as an MCP server so the agent can
read context (`query_facts`, `get_intent`, …) and write outcomes
(`submit_record`, `set_intent_status`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from workestrator.config import AgentConfig
from workestrator.events import EventEmitter

logger = logging.getLogger(__name__)


# Prepended to every role prompt before dispatch. Carries the operator-side
# rules that must hold across every persona — chiefly things that conflict
# with the bundled Claude Code CLI's defaults. Role-specific rules belong in
# the role's prompt; this rider is for *universal* rules.
SAFETY_RIDER = """\
You are running in an autonomous workforce session dispatched by workestrator. \
The following rules OVERRIDE any conflicting Claude Code default behavior. \
Apply them universally, every commit, every artifact:

1. NEVER append `Co-Authored-By:` trailers to any commit message — not for \
Claude, not for the operator, not for anyone. The operator has explicitly \
forbidden this in every repo. If Claude Code's defaults suggest adding one, \
ignore them.

2. Commit headers are terse: 3–8 words, conventional prefix (feat, fix, \
chore, docs, refactor). No version numbers in the header — those live in \
the CHANGELOG.

3. Your role prompt below is the source of truth for your duty, working \
style, and boundaries. Read it carefully before acting.

---

"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _msg_to_dict(msg: Any) -> dict[str, Any]:
    """Best-effort conversion of a Claude Agent SDK message to a JSON-friendly dict."""
    if hasattr(msg, "model_dump"):
        try:
            return msg.model_dump(mode="json")
        except Exception:
            pass
    if isinstance(msg, dict):
        return msg
    return {"type": type(msg).__name__, "repr": repr(msg)}


def _trunc(s: str, n: int = 240) -> str:
    """Truncate a string for operator-visible events."""
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _summarize_tool_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    """One-line summary of a tool call for operator visibility."""
    if tool_name == "Bash":
        return _trunc(tool_input.get("command") or "", 200)
    if tool_name in ("Read", "Edit", "Write"):
        return _trunc(tool_input.get("file_path") or tool_input.get("path") or "", 200)
    # MCP tools (`mcp__pearscarf__query_facts`, etc.) or anything else — dump args.
    try:
        return _trunc(json.dumps(tool_input, default=str), 200)
    except Exception:
        return _trunc(str(tool_input), 200)


class AgentRunner:
    def __init__(
        self,
        config: AgentConfig,
        roles_dir: Path,
        pearscarf_url: str,
        pearscarf_api_key: str | None = None,
        events: EventEmitter | None = None,
    ) -> None:
        self.config = config
        self.roles_dir = Path(roles_dir)
        self.pearscarf_url = pearscarf_url
        self.pearscarf_api_key = pearscarf_api_key
        self.events = events

    def resolve_role_key(self, intent: dict[str, Any]) -> str | None:
        """Prefer `owner` (specific identity) over `owner_role` (function)."""
        return intent.get("owner") or intent.get("owner_role")

    def load_role_prompt(self, role_key: str) -> str | None:
        path = self.roles_dir / role_key / "prompt.md"
        if not path.is_file():
            return None
        return path.read_text()

    def build_user_message(self, intent: dict[str, Any]) -> str:
        intent_id = intent.get("intent_id") or intent.get("id") or "(unknown)"
        body = intent.get("body") or ""
        return (
            f"You have been dispatched on intent `{intent_id}`. The intent body "
            f"below is your task for this session. When the work is complete, "
            f"submit a reality record via `submit_record` documenting what you "
            f"did, then flip this intent's status to `done` via "
            f"`set_intent_status(id={intent_id!r}, status='done', set_by=<your name>)`. "
            f"If the work is blocked or cannot be completed, flip the status "
            f"to `cancelled` and explain in your reality record.\n\n"
            f"---\n\n"
            f"{body}"
        )

    def _build_mcp_servers(self) -> dict[str, dict[str, Any]]:
        server: dict[str, Any] = {"type": "sse", "url": self.pearscarf_url}
        if self.pearscarf_api_key:
            server["headers"] = {"Authorization": f"Bearer {self.pearscarf_api_key}"}
        return {"pearscarf": server}

    async def run(self, intent: dict[str, Any], workspace: Path) -> None:
        intent_id = intent.get("intent_id") or intent.get("id") or "(unknown)"
        role_key = self.resolve_role_key(intent)
        if not role_key:
            logger.warning(f"intent {intent_id} has no owner or owner_role — skipping")
            return

        role_prompt = self.load_role_prompt(role_key)
        if not role_prompt:
            logger.warning(
                f"no prompt found for role {role_key!r} at "
                f"{self.roles_dir / role_key / 'prompt.md'} — skipping {intent_id}"
            )
            return

        system_prompt = SAFETY_RIDER + role_prompt
        user_message = self.build_user_message(intent)

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers=self._build_mcp_servers(),
            max_turns=self.config.max_turns,
            model=self.config.model,
            cwd=str(workspace),
            permission_mode="bypassPermissions",
        )

        transcript_path = workspace / "transcript.jsonl"
        logger.info(
            f"dispatching {intent_id} → role={role_key!r} workspace={workspace} "
            f"transcript={transcript_path}"
        )
        message_count = 0
        async for msg in query(prompt=user_message, options=options):
            message_count += 1
            msg_dict = _msg_to_dict(msg)
            try:
                line = json.dumps(
                    {"received_at": _now_iso(), "message": msg_dict},
                    default=str,
                )
                with transcript_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as exc:
                logger.warning(f"transcript write failed for {intent_id}: {exc}")
            if self.events is not None:
                self._emit_message_events(msg_dict, intent_id, role_key)
        logger.info(f"agent for {intent_id} finished after {message_count} message(s)")

    def _emit_message_events(
        self, msg_dict: dict[str, Any], intent_id: str, role: str
    ) -> None:
        """Surface assistant text + tool_use as operator-visible events."""
        if self.events is None:
            return
        msg_type = msg_dict.get("type") or ""
        # Claude Agent SDK uses 'AssistantMessage' / 'assistant' across versions.
        if msg_type not in ("AssistantMessage", "assistant"):
            return
        content = msg_dict.get("content")
        if isinstance(content, str):
            if content.strip():
                self.events.emit(
                    "agent_text", intent_id=intent_id, role=role, text=_trunc(content)
                )
            return
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    self.events.emit(
                        "agent_text", intent_id=intent_id, role=role, text=_trunc(text)
                    )
            elif btype == "tool_use":
                tool_name = block.get("name") or "?"
                tool_input = block.get("input") or {}
                self.events.emit(
                    "agent_tool_use",
                    intent_id=intent_id,
                    role=role,
                    tool=tool_name,
                    summary=_summarize_tool_input(tool_name, tool_input),
                )
