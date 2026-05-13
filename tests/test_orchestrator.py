"""Tests for the orchestration loop's pure logic.

We mock `PearscarfClient` and `AgentRunner` so these run without docker
or network. Coverage focuses on dispatch eligibility, concurrency gating,
and dependency satisfaction.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from workestrator.config import (
    AgentConfig,
    Config,
    EventsConfig,
    OrchestratorConfig,
    PearscarfConfig,
    RolesConfig,
    WorkspaceConfig,
)
from workestrator.orchestrator import Workestrator, _intent_id


def _config(tmp_path: Path, concurrency: int = 2) -> Config:
    return Config(
        pearscarf=PearscarfConfig(mcp_url="http://test"),
        orchestrator=OrchestratorConfig(
            poll_interval_seconds=1, max_concurrent_agents=concurrency
        ),
        roles=RolesConfig(dir=tmp_path / "roles"),
        agent=AgentConfig(),
        workspace=WorkspaceConfig(dir=tmp_path / "ws"),
        events=EventsConfig(log_path=tmp_path / "events.jsonl"),
    )


class FakePearscarf:
    """Records calls; returns canned data."""

    def __init__(self, intents: list[dict], intent_lookup: dict[str, dict] | None = None) -> None:
        self.intents = intents
        self.intent_lookup = intent_lookup or {}
        self.status_calls: list[tuple[str, str]] = []

    async def query_intents(self, **_kwargs):
        return self.intents

    async def get_intent(self, intent_id: str, with_children: bool = False):
        return self.intent_lookup.get(intent_id)

    async def set_intent_status(self, intent_id: str, status: str, set_by: str = "workestrator"):
        self.status_calls.append((intent_id, status))
        return {"intent_id": intent_id, "status": status}


class FakeRunner:
    """Records dispatch calls; never spawns Claude."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, intent: dict, workspace: Path) -> None:
        self.calls.append(_intent_id(intent))


def test_intent_id_accepts_either_field() -> None:
    assert _intent_id({"intent_id": "a"}) == "a"
    assert _intent_id({"id": "b"}) == "b"


def test_intent_id_raises_when_missing() -> None:
    with pytest.raises(ValueError):
        _intent_id({"owner": "hex"})


def test_tick_dispatches_eligible_intent(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path, concurrency=2)
        w = Workestrator(cfg)
        ps = FakePearscarf(intents=[{"intent_id": "i1", "owner": "hex", "body": "x"}])
        runner = FakeRunner()
        await w.tick(ps, runner)
        # The dispatch task was created — await it.
        await asyncio.gather(*w._running.values(), return_exceptions=True)
        assert runner.calls == ["i1"]
        assert ("i1", "in_progress") in ps.status_calls

    asyncio.run(run())


def test_tick_respects_concurrency_cap(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path, concurrency=1)
        w = Workestrator(cfg)
        ps = FakePearscarf(
            intents=[
                {"intent_id": "i1", "owner": "hex", "body": "x"},
                {"intent_id": "i2", "owner": "anton", "body": "y"},
            ]
        )
        runner = FakeRunner()
        await w.tick(ps, runner)
        await asyncio.gather(*w._running.values(), return_exceptions=True)
        # Only the first should have been dispatched.
        assert runner.calls == ["i1"]

    asyncio.run(run())


def test_tick_gates_on_depends_on(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        # i2 depends on i1; i1 is not done — i2 must be skipped.
        ps = FakePearscarf(
            intents=[
                {"intent_id": "i2", "owner": "anton", "body": "y", "depends_on": ["i1"]},
            ],
            intent_lookup={"i1": {"intent_id": "i1", "status": "in_progress"}},
        )
        runner = FakeRunner()
        await w.tick(ps, runner)
        await asyncio.gather(*w._running.values(), return_exceptions=True)
        assert runner.calls == []

    asyncio.run(run())


def test_tick_dispatches_when_all_deps_done(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        ps = FakePearscarf(
            intents=[
                {
                    "intent_id": "i3",
                    "owner": "cindy",
                    "body": "z",
                    "depends_on": ["i1", "i2"],
                },
            ],
            intent_lookup={
                "i1": {"intent_id": "i1", "status": "done"},
                "i2": {"intent_id": "i2", "status": "done"},
            },
        )
        runner = FakeRunner()
        await w.tick(ps, runner)
        await asyncio.gather(*w._running.values(), return_exceptions=True)
        assert runner.calls == ["i3"]

    asyncio.run(run())


def test_tick_emits_dispatched_and_completion_events(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        # intent_lookup makes _dispatch's get_intent return done, so the
        # finally block emits intent_completed rather than intent_failed.
        ps = FakePearscarf(
            intents=[
                {
                    "intent_id": "i1",
                    "owner": "hex",
                    "owner_role": "head-eng",
                    "title": "ship X",
                    "body": "x",
                }
            ],
            intent_lookup={"i1": {"intent_id": "i1", "status": "done"}},
        )
        runner = FakeRunner()
        await w.tick(ps, runner)
        await asyncio.gather(*w._running.values(), return_exceptions=True)

        lines = cfg.events.log_path.read_text().strip().split("\n")
        events = [json.loads(line) for line in lines]
        kinds = [e["event"] for e in events]
        assert "intent_dispatched" in kinds
        assert "intent_completed" in kinds
        dispatched = next(e for e in events if e["event"] == "intent_dispatched")
        assert dispatched["intent_id"] == "i1"
        assert dispatched["role"] == "head-eng"
        assert dispatched["owner"] == "hex"
        assert dispatched["title"] == "ship X"

    asyncio.run(run())


def test_tick_skips_already_running_intents(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        # Inject a fake-running task to simulate an in-flight dispatch.
        async def never() -> None:
            await asyncio.sleep(10)

        running_task = asyncio.create_task(never())
        w._running["i1"] = running_task

        try:
            ps = FakePearscarf(intents=[{"intent_id": "i1", "owner": "hex", "body": "x"}])
            runner = FakeRunner()
            await w.tick(ps, runner)
            # No new dispatch — the in-flight one is still going.
            assert runner.calls == []
        finally:
            running_task.cancel()
            await asyncio.gather(running_task, return_exceptions=True)

    asyncio.run(run())
