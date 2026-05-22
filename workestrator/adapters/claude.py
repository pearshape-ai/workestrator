"""Claude adapter — spawns `claude --print` and parses its stream-json output.

Replaces the prior claude-agent-sdk wrapper (which is becoming paid). Per-intent
dispatch knobs come from `intent.runtime_config`:

- `chrome_required` (bool) → `--chrome`
- `model` (str) → `--model <name>`
- `max_budget_usd` (float) → `--max-budget-usd <amount>`

MCP servers wire pearscarf as the default (via the workestrator-level URL +
optional auth). Other MCP servers in `runtime_config.mcp_servers` will be
honored by future iterations; today claude-in-chrome ships via `--chrome`
and pearscarf via `--mcp-config`.

Output parsing: `claude --print --output-format stream-json --verbose` emits
one JSON object per line. We translate `assistant` events to the shape the
workestrator core's per-message event emitter expects (top-level `type` +
`content` array), and pass other event types (`system`, `user` tool_results,
`result`, `rate_limit_event`) through unchanged for the transcript.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from workestrator.config import AgentConfig

logger = logging.getLogger(__name__)


def _stream_event_to_message_dict(ev: dict[str, Any]) -> dict[str, Any]:
    """Translate a stream-json event to a normalized message dict.

    Today the workestrator core's `_emit_message_events` only inspects the
    `type` + top-level `content` array on assistant messages (matching the
    prior SDK shape). We lift stream-json's `message.content` up to the top
    level for `assistant` events so the core stays unchanged. Other event
    types pass through as-is — they still land in transcripts; richer event
    emission (e.g. `agent_tool_result`) is a future commit.
    """
    if ev.get("type") == "assistant":
        return {
            "type": "assistant",
            "content": ev.get("message", {}).get("content", []),
        }
    return ev


class ClaudeAdapter:
    """Dispatches an intent via the `claude` CLI in headless (--print) mode."""

    def __init__(
        self,
        config: AgentConfig,
        pearscarf_url: str,
        pearscarf_api_key: str | None = None,
    ) -> None:
        self.config = config
        self.pearscarf_url = pearscarf_url
        self.pearscarf_api_key = pearscarf_api_key

    def _build_mcp_config(self) -> str:
        """JSON string for the `--mcp-config` flag — pearscarf only for now."""
        server: dict[str, Any] = {"type": "http", "url": self.pearscarf_url}
        if self.pearscarf_api_key:
            server["headers"] = {"Authorization": f"Bearer {self.pearscarf_api_key}"}
        return json.dumps({"mcpServers": {"pearscarf": server}})

    def _build_command(
        self,
        runtime_config: dict[str, Any],
        system_prompt: str,
        user_message: str,
    ) -> list[str]:
        """Assemble the `claude --print` argv. Read knobs from runtime_config;
        fall back to AgentConfig defaults where the runtime_config is silent."""
        model = runtime_config.get("model") or self.config.model
        cmd = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",  # required by --output-format=stream-json
            "--input-format",
            "text",
            "--permission-mode",
            "bypassPermissions",
            "--no-session-persistence",
            "--system-prompt",
            system_prompt,
            "--mcp-config",
            self._build_mcp_config(),
            "--model",
            model,
        ]
        if runtime_config.get("chrome_required"):
            cmd.append("--chrome")
        if (budget := runtime_config.get("max_budget_usd")) is not None:
            cmd.extend(["--max-budget-usd", str(budget)])
        cmd.append(user_message)
        return cmd

    async def dispatch(
        self,
        intent: dict[str, Any],
        workspace: Path,
        system_prompt: str,
        user_message: str,
    ) -> AsyncIterator[dict[str, Any]]:
        runtime_config = intent.get("runtime_config") or {}
        cmd = self._build_command(runtime_config, system_prompt, user_message)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
        )
        try:
            assert proc.stdout is not None  # PIPE is set
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "claude adapter: non-json stdout line (truncated): %s",
                        line[:200],
                    )
                    continue
                yield _stream_event_to_message_dict(ev)
            rc = await proc.wait()
            if rc != 0 and proc.stderr is not None:
                stderr_bytes = await proc.stderr.read()
                logger.warning(
                    "claude adapter: subprocess exited %d; stderr (truncated): %s",
                    rc,
                    stderr_bytes[:500].decode("utf-8", errors="replace"),
                )
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
