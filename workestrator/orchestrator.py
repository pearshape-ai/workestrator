"""The orchestration loop.

Polls pearscarf for `todo` intents, gates on `depends_on`, claims one per
available slot by flipping status to `in_progress`, spawns a Claude agent
per claim. The agent owns the actual transition to `done` (or `cancelled`)
via its own MCP calls — the orchestrator just dispatches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from workestrator.agent_runner import AgentRunner
from workestrator.config import Config
from workestrator.events import EventEmitter
from workestrator.pearscarf_client import PearscarfClient
from workestrator.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class Workestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.workspace = WorkspaceManager(config.workspace.dir)
        self.events = EventEmitter(config.events.log_path)
        self._running: dict[str, asyncio.Task] = {}
        # Monotonic timestamp of the most-recent dispatch per intent. Used to
        # debounce coordinator wakes — see `_wake_coordinators`.
        self._last_wake: dict[str, float] = {}

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
                events=self.events,
            )
            self.events.emit(
                "daemon_started",
                poll_interval_seconds=self.config.orchestrator.poll_interval_seconds,
                max_concurrent_agents=self.config.orchestrator.max_concurrent_agents,
            )
            try:
                while True:
                    await self.tick(pearscarf, agent_runner)
                    await asyncio.sleep(self.config.orchestrator.poll_interval_seconds)
            except asyncio.CancelledError:
                logger.info("orchestrator cancelled — waiting for running agents")
                self.events.emit("daemon_stopping", in_flight=len(self._running))
                for task in self._running.values():
                    task.cancel()
                if self._running:
                    await asyncio.gather(*self._running.values(), return_exceptions=True)
                raise

    async def tick(self, pearscarf: PearscarfClient, agent_runner: AgentRunner) -> None:
        """One poll-and-dispatch pass."""
        # Reap finished work.
        self._running = {iid: t for iid, t in self._running.items() if not t.done()}

        # Wake-eligible coordinators get priority over new todos: a stuck
        # coordinator gates its whole subtree, so resolving its next step
        # pushes the system forward.
        await self._wake_coordinators(pearscarf, agent_runner)

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
            self.events.emit("intent_dispatched", **_intent_summary(intent))
            self._last_wake[intent_id] = time.monotonic()
            self._running[intent_id] = asyncio.create_task(
                self._dispatch(intent, agent_runner, pearscarf)
            )

    async def _wake_coordinators(
        self, pearscarf: PearscarfClient, agent_runner: AgentRunner
    ) -> None:
        """Re-dispatch any in_progress coordinator with zero in-flight children
        whose last wake is past the debounce window.

        A coordinator's session is stateless: each wake re-reads graph state
        (its own intent body, children's statuses) and decides what to do next.
        Workestrator's job is to keep waking it whenever there's nothing in
        flight to wait on — until the coordinator itself flips status to
        `done` or `cancelled`.
        """
        slots = self.config.orchestrator.max_concurrent_agents - len(self._running)
        if slots <= 0:
            return

        coordinators = await pearscarf.query_intents(
            status="in_progress", type="coordinator"
        )
        if not coordinators:
            return

        now = time.monotonic()
        debounce = self.config.orchestrator.coordinator_wake_debounce_seconds

        for intent in coordinators:
            if slots <= 0:
                break
            intent_id = _intent_id(intent)
            if intent_id in self._running:
                continue  # currently dispatched; will naturally complete on its own
            last = self._last_wake.get(intent_id)
            if last is not None and (now - last) < debounce:
                continue  # too soon since last wake
            children = await pearscarf.query_intents(parent=intent_id)
            in_flight = [
                c for c in (children or []) if c.get("status") in ("todo", "in_progress")
            ]
            if in_flight:
                continue  # naturally woken when last child completes

            self.events.emit("coordinator_woken", **_intent_summary(intent))
            self._last_wake[intent_id] = now
            self._running[intent_id] = asyncio.create_task(
                self._dispatch(intent, agent_runner, pearscarf)
            )
            slots -= 1

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
        summary = _intent_summary(intent)
        timeout = _resolve_max_duration(intent, self.config.agent.default_max_duration_seconds)
        raised: Exception | None = None
        timed_out = False
        try:
            await asyncio.wait_for(
                agent_runner.run(intent, workspace=workspace),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"agent for {intent_id} exceeded max_duration={timeout}s — terminating"
            )
            timed_out = True
        except Exception as exc:
            logger.exception(f"agent for {intent_id} raised: {exc}")
            raised = exc
        finally:
            check = await pearscarf.get_intent(intent_id)
            final_status = (check or {}).get("status")
            is_coordinator = intent.get("intent_type") == "coordinator"
            if final_status in ("done", "cancelled"):
                self.events.emit("intent_completed", **summary, final_status=final_status)
            elif is_coordinator and not timed_out and raised is None:
                # Normal coordinator exit: dispatched children (or decided to
                # wait) and returned cleanly without flipping its own status.
                # Leave it in_progress — `_wake_coordinators` will re-dispatch
                # when a child completes or the debounce window elapses.
                self.events.emit("coordinator_paused", **summary)
            else:
                # Executor exit-without-done, or any timeout / crash. Force-
                # cancel so the next poll doesn't re-claim, and surface why.
                if timed_out:
                    reason = f"timeout after {timeout}s"
                elif raised is not None:
                    reason = f"agent raised: {raised}"
                else:
                    reason = (
                        f"agent returned without terminal status "
                        f"(last seen: {final_status!r})"
                    )
                try:
                    await pearscarf.set_intent_status(
                        intent_id, "cancelled", set_by="workestrator"
                    )
                except Exception as flip_exc:
                    logger.warning(
                        f"force-cancel for {intent_id} failed: {flip_exc} — "
                        "intent may re-dispatch"
                    )
                self.events.emit("intent_failed", **summary, error=reason)


def _resolve_max_duration(intent: dict[str, Any], default: int) -> int:
    """Per-intent timeout override lives in `runtime_config.max_duration_seconds`;
    fall back to the orchestrator-side default."""
    rc = intent.get("runtime_config") or {}
    value = rc.get("max_duration_seconds")
    if value is None:
        return int(default)
    return int(value)


def _intent_id(intent: dict[str, Any]) -> str:
    """MCP responses use `intent_id`; storage uses `id`. Accept either."""
    iid = intent.get("intent_id") or intent.get("id")
    if not iid:
        raise ValueError(f"intent missing id field: {intent!r}")
    return str(iid)


def _intent_summary(intent: dict[str, Any]) -> dict[str, Any]:
    """Fields commonly surfaced on intent_* events."""
    return {
        "intent_id": _intent_id(intent),
        "role": intent.get("owner_role"),
        "owner": intent.get("owner"),
        "title": intent.get("title"),
    }
