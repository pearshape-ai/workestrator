"""Spawn one Claude agent session per intent.

The agent's system prompt is the role manifest — `soul.md` + `skills.md`
under `<roles_dir>/<owner_role>/` — optionally prepended with a shared
`<roles_dir>/_pearscarf.md` foundation. Its user message is the intent
body. Pearscarf is attached as an MCP server so the agent can read context
(`query_facts`, `get_intent`, …) and write outcomes (`submit_record`,
`set_intent_status`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workestrator.adapters import Adapter, ClaudeAdapter
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
        # Runtime → adapter instance. Dispatched intents are routed via
        # `intent.runtime`. Add new adapters here as runtimes are supported.
        self._adapters: dict[str, Adapter] = {
            "claude": ClaudeAdapter(
                config=config,
                pearscarf_url=pearscarf_url,
                pearscarf_api_key=pearscarf_api_key,
            ),
        }

    def get_adapter(self, runtime: str) -> Adapter:
        """Resolve a runtime selector to its adapter. Fails loud on unknown."""
        if runtime not in self._adapters:
            raise ValueError(
                f"no adapter registered for runtime={runtime!r}; "
                f"known: {sorted(self._adapters)}"
            )
        return self._adapters[runtime]

    def resolve_role_key(self, intent: dict[str, Any]) -> str | None:
        """Resolve the directory key under `roles_dir`.

        The role's canonical path slug is `owner_role` (e.g. `gtm`,
        `gtm/linkedin-prospecting`). `owner` is the agent's identity for
        audit/display (e.g. `Greg`, `Linus`) — never a directory key. Fall
        back to `owner` only if `owner_role` is missing.
        """
        return intent.get("owner_role") or intent.get("owner")

    def load_role_prompt(
        self,
        role_key: str,
        *,
        intent_type: str | None = None,
    ) -> str | None:
        """Assemble the dispatched session's system prompt.

        Concatenates `<roles_dir>/<role>/soul.md` + `<roles_dir>/<role>/skills.md`,
        plus an optional `<roles_dir>/<role>/op-delta.md` (the role's record-grain
        carve-out, appended after skills if present — declares the role's
        specific operational-delta atom on top of the general rule in
        `_pearscarf.md`). Both soul and skills must exist; returns None
        otherwise (dispatch will skip the intent).

        Optional shared files may be prepended in order:

        - `<roles_dir>/_pearscarf.md` — universal PearScarf foundation (every role).
        - `<roles_dir>/_psc_consumer.md` — the read discipline: how to query
          PearScarf well (every role, so it's in the agent's context before its
          first query — no fetch step to forget).
        - `<roles_dir>/_coordinator.md` — coordinator-runtime contract (only when
          `intent_type == "coordinator"`). Defines the wake protocol: every
          session, query children first; decide based on existing state; never
          treat a wake as a fresh dispatch.
        """
        role_dir = self.roles_dir / role_key
        soul = role_dir / "soul.md"
        skills = role_dir / "skills.md"
        if not (soul.is_file() and skills.is_file()):
            return None

        parts: list[str] = []
        shared = self.roles_dir / "_pearscarf.md"
        if shared.is_file():
            parts.append(shared.read_text())
        consumer = self.roles_dir / "_psc_consumer.md"
        if consumer.is_file():
            parts.append(consumer.read_text())
        if intent_type == "coordinator":
            coord = self.roles_dir / "_coordinator.md"
            if coord.is_file():
                parts.append(coord.read_text())
        parts.append(soul.read_text() + "\n\n---\n\n" + skills.read_text())
        op_delta = role_dir / "op-delta.md"
        if op_delta.is_file():
            parts.append(op_delta.read_text())
        return "\n\n---\n\n".join(parts)

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

    async def run(self, intent: dict[str, Any], workspace: Path) -> None:
        intent_id = intent.get("intent_id") or intent.get("id") or "(unknown)"
        role_key = self.resolve_role_key(intent)
        if not role_key:
            # Raise rather than silently return — the orchestrator's
            # _dispatch finally block treats a clean return on an
            # in_progress coordinator as a "natural pause" and emits
            # coordinator_paused, which would be misleading here (we
            # never actually dispatched anything).
            raise ValueError(
                f"intent {intent_id} has no owner or owner_role — "
                "cannot resolve a role directory"
            )

        role_prompt = self.load_role_prompt(
            role_key, intent_type=intent.get("intent_type")
        )
        if not role_prompt:
            raise FileNotFoundError(
                f"no role manifest at {self.roles_dir / role_key}/ "
                f"(expected soul.md + skills.md) for intent {intent_id}"
            )

        runtime = intent.get("runtime")
        if not runtime:
            logger.error(
                f"intent {intent_id} has no `runtime` field — skipping. "
                "Ensure pearscarf >= 1.39.1 (which adds the runtime envelope)."
            )
            return
        try:
            adapter = self.get_adapter(runtime)
        except ValueError as exc:
            logger.error(f"intent {intent_id}: {exc} — skipping")
            return

        system_prompt = SAFETY_RIDER + role_prompt
        user_message = self.build_user_message(intent)

        transcript_path = workspace / "transcript.jsonl"
        logger.info(
            f"dispatching {intent_id} → role={role_key!r} runtime={runtime!r} "
            f"workspace={workspace} transcript={transcript_path}"
        )
        message_count = 0
        async for msg_dict in adapter.dispatch(
            intent=intent,
            workspace=workspace,
            system_prompt=system_prompt,
            user_message=user_message,
        ):
            message_count += 1
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
