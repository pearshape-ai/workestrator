"""Spawn one Claude agent session per intent.

The agent's system prompt is the role's `prompt.md`; its user message is
the intent body. Pearscarf is attached as an MCP server so the agent can
read context (`query_facts`, `get_intent`, …) and write outcomes
(`submit_record`, `set_intent_status`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from workestrator.config import AgentConfig

logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(
        self,
        config: AgentConfig,
        roles_dir: Path,
        pearscarf_url: str,
        pearscarf_api_key: str | None = None,
    ) -> None:
        self.config = config
        self.roles_dir = Path(roles_dir)
        self.pearscarf_url = pearscarf_url
        self.pearscarf_api_key = pearscarf_api_key

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

        system_prompt = self.load_role_prompt(role_key)
        if not system_prompt:
            logger.warning(
                f"no prompt found for role {role_key!r} at "
                f"{self.roles_dir / role_key / 'prompt.md'} — skipping {intent_id}"
            )
            return

        user_message = self.build_user_message(intent)

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers=self._build_mcp_servers(),
            max_turns=self.config.max_turns,
            model=self.config.model,
            cwd=str(workspace),
        )

        logger.info(f"dispatching {intent_id} → role={role_key!r} workspace={workspace}")
        message_count = 0
        async for _msg in query(prompt=user_message, options=options):
            message_count += 1
        logger.info(f"agent for {intent_id} finished after {message_count} message(s)")
