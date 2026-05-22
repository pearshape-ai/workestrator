"""Thin MCP client for pearscarf.

Wraps the streamable-HTTP transport from the `mcp` SDK and exposes the
handful of pearscarf tools the orchestrator actually calls. JSON tool-result
payloads are decoded so callers see plain dicts.

Pearscarf 1.40.1 retired SSE in favor of streamable-HTTP at `/mcp`; this
client was switched at the same time. The transport returns a 3-tuple
(read, write, get_session_id); we ignore the third value — pearscarf
doesn't require session-id propagation from this client.
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


class PearscarfClient:
    """Async context-managed MCP client. One instance per orchestrator run."""

    def __init__(self, url: str, api_key: str | None = None) -> None:
        self.url = url
        self.api_key = api_key
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> PearscarfClient:
        self._stack = AsyncExitStack()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(self.url, headers=headers)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        logger.info(f"connected to pearscarf MCP at {self.url}")
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._session = None
        self._stack = None

    async def _call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("PearscarfClient used outside an async-with block")
        result = await self._session.call_tool(tool, args or {})
        if not result.content:
            return {}
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        if not text_blocks:
            return {}
        try:
            return json.loads(text_blocks[0])
        except json.JSONDecodeError:
            # Some tools return plain markdown (e.g. resource fetches). Bare-text fall-back.
            return {"text": text_blocks[0]}

    async def query_intents(
        self,
        status: str | None = None,
        parent: str | None = None,
        type: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"limit": limit}
        if status is not None:
            args["status"] = status
        if parent is not None:
            args["parent"] = parent
        if type is not None:
            args["type"] = type
        if since is not None:
            args["since"] = since
        result = await self._call("query_intents", args)
        return result.get("intents", [])

    async def get_intent(self, intent_id: str, with_children: bool = False) -> dict[str, Any] | None:
        result = await self._call("get_intent", {"id": intent_id, "with_children": with_children})
        if result.get("error") == "not_found":
            return None
        return result

    async def set_intent_status(
        self, intent_id: str, status: str, set_by: str = "workestrator"
    ) -> dict[str, Any]:
        return await self._call(
            "set_intent_status",
            {"id": intent_id, "status": status, "set_by": set_by},
        )
