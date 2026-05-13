"""The orchestration loop.

Polls pearscarf for `todo` intents, gates on `depends_on`, claims one per
available slot by flipping status to `in_progress`, spawns a Claude agent
per claim. The agent owns the actual transition to `done` (or `cancelled`)
via its own MCP calls — the orchestrator just dispatches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from workestrator.agent_runner import AgentRunner
from workestrator.config import Config
from workestrator.pearscarf_client import PearscarfClient
from workestrator.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class Workestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.workspace = WorkspaceManager(config.workspace.dir)
        self._running: dict[str, asyncio.Task] = {}

    async def run(self) -> None:
        """Run the orchestrator loop until cancelled."""
        async with PearscarfClient(
            url=self.config.pearscarf.mcp_url,
            api_key=self.config.pearscarf.api_key,
        ) as pearscarf:
            agent_runner = AgentRunner(
                config=self.config.agent,
                roles_dir=self.config.roles.dir,
                pearscarf_url=self.config.pearscarf.mcp_url,
                pearscarf_api_key=self.config.pearscarf.api_key,
            )
            try:
                while True:
                    await self.tick(pearscarf, agent_runner)
                    await asyncio.sleep(self.config.orchestrator.poll_interval_seconds)
            except asyncio.CancelledError:
                logger.info("orchestrator cancelled — waiting for running agents")
                for task in self._running.values():
                    task.cancel()
                if self._running:
                    await asyncio.gather(*self._running.values(), return_exceptions=True)
                raise

    async def tick(self, pearscarf: PearscarfClient, agent_runner: AgentRunner) -> None:
        """One poll-and-dispatch pass."""
        # Reap finished work.
        self._running = {iid: t for iid, t in self._running.items() if not t.done()}

        slots = self.config.orchestrator.max_concurrent_agents - len(self._running)
        if slots <= 0:
            return

        candidates = await pearscarf.query_intents(status="todo")
        if not candidates:
            return

        eligible: list[dict[str, Any]] = []
        for intent in candidates:
            intent_id = _intent_id(intent)
            if intent_id in self._running:
                continue
            if not await self._deps_satisfied(pearscarf, intent):
                continue
            eligible.append(intent)
            if len(eligible) >= slots:
                break

        for intent in eligible:
            intent_id = _intent_id(intent)
            logger.info(f"claiming {intent_id}")
            await pearscarf.set_intent_status(intent_id, "in_progress", set_by="workestrator")
            self._running[intent_id] = asyncio.create_task(
                self._dispatch(intent, agent_runner, pearscarf)
            )

    async def _deps_satisfied(self, pearscarf: PearscarfClient, intent: dict[str, Any]) -> bool:
        deps = intent.get("depends_on") or []
        for dep_id in deps:
            dep = await pearscarf.get_intent(dep_id)
            if not dep or dep.get("status") != "done":
                return False
        return True

    async def _dispatch(
        self,
        intent: dict[str, Any],
        agent_runner: AgentRunner,
        pearscarf: PearscarfClient,
    ) -> None:
        intent_id = _intent_id(intent)
        workspace = self.workspace.for_intent(intent_id)
        try:
            await agent_runner.run(intent, workspace=workspace)
        except Exception as exc:
            logger.exception(f"agent for {intent_id} raised: {exc}")
        finally:
            check = await pearscarf.get_intent(intent_id)
            final_status = (check or {}).get("status")
            if final_status not in ("done", "cancelled"):
                logger.warning(
                    f"agent for {intent_id} returned but status={final_status!r} — "
                    "agent didn't flip it; orchestrator leaves it for the next poll"
                )


def _intent_id(intent: dict[str, Any]) -> str:
    """MCP responses use `intent_id`; storage uses `id`. Accept either."""
    iid = intent.get("intent_id") or intent.get("id")
    if not iid:
        raise ValueError(f"intent missing id field: {intent!r}")
    return str(iid)
