"""Claude adapter — wraps the claude-agent-sdk `query()` async iterator.

This initial version preserves the existing SDK behavior verbatim so the
adapter scaffolding lands as a pure refactor. A follow-up commit replaces
the internals with `claude --print` subprocess + stream-json parsing,
reading `intent.runtime_config` for per-intent dispatch knobs
(`chrome_required`, `mcp_servers`, `model`, …).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query

from workestrator.config import AgentConfig


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


class ClaudeAdapter:
    """Dispatches an intent via the claude-agent-sdk SDK."""

    def __init__(
        self,
        config: AgentConfig,
        pearscarf_url: str,
        pearscarf_api_key: str | None = None,
    ) -> None:
        self.config = config
        self.pearscarf_url = pearscarf_url
        self.pearscarf_api_key = pearscarf_api_key

    def _build_mcp_servers(self) -> dict[str, dict[str, Any]]:
        server: dict[str, Any] = {"type": "sse", "url": self.pearscarf_url}
        if self.pearscarf_api_key:
            server["headers"] = {"Authorization": f"Bearer {self.pearscarf_api_key}"}
        return {"pearscarf": server}

    async def dispatch(
        self,
        intent: dict[str, Any],
        workspace: Path,
        system_prompt: str,
        user_message: str,
    ) -> AsyncIterator[dict[str, Any]]:
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers=self._build_mcp_servers(),
            max_turns=self.config.max_turns,
            model=self.config.model,
            cwd=str(workspace),
            permission_mode="bypassPermissions",
        )
        async for msg in query(prompt=user_message, options=options):
            yield _msg_to_dict(msg)
