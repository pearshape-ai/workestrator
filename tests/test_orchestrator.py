"""Tests for the orchestration loop's pure logic.

We mock `PearscarfClient` and `AgentRunner` so these run without docker
or network. Coverage focuses on dispatch eligibility, concurrency gating,
and dependency satisfaction.
"""

from __future__ import annotations

import asyncio
import json
import time
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
from workestrator.orchestrator import Workestrator, _intent_id, _resolve_max_duration


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
    """Records calls; returns canned data with minimal filter support."""

    def __init__(self, intents: list[dict], intent_lookup: dict[str, dict] | None = None) -> None:
        self.intents = intents
        self.intent_lookup = intent_lookup or {}
        self.status_calls: list[tuple[str, str]] = []

    async def query_intents(self, **kwargs):
        """Honor status / intent_type / parent filters; missing fields use the
        same defaults pearscarf would (`status="todo"`, `intent_type="executor"`)
        so test intents stay minimal."""
        results = self.intents
        if (s := kwargs.get("status")) is not None:
            results = [i for i in results if (i.get("status") or "todo") == s]
        if (t := kwargs.get("intent_type")) is not None:
            results = [i for i in results if (i.get("intent_type") or "executor") == t]
        if (p := kwargs.get("parent")) is not None:
            results = [i for i in results if i.get("parent_record_id") == p]
        return results

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


def test_resolve_max_duration_uses_runtime_config_override() -> None:
    intent = {"runtime_config": {"max_duration_seconds": 120}}
    assert _resolve_max_duration(intent, default=3600) == 120


def test_resolve_max_duration_falls_back_to_default() -> None:
    assert _resolve_max_duration({}, default=3600) == 3600
    assert _resolve_max_duration({"runtime_config": {}}, default=3600) == 3600
    assert _resolve_max_duration({"runtime_config": {"model": "sonnet"}}, default=3600) == 3600


def test_resolve_max_duration_coerces_strings_to_int() -> None:
    intent = {"runtime_config": {"max_duration_seconds": "1800"}}
    assert _resolve_max_duration(intent, default=3600) == 1800


class _StuckRunner:
    """Agent finishes normally but never flips the intent status."""

    async def run(self, intent: dict, workspace: Path) -> None:
        return


class _TimeoutRunner:
    """Agent runs longer than the wait_for timeout (never returns)."""

    async def run(self, intent: dict, workspace: Path) -> None:
        await asyncio.sleep(10)


class _RaisingRunner:
    """Agent crashes mid-flight."""

    async def run(self, intent: dict, workspace: Path) -> None:
        raise RuntimeError("boom")


def test_dispatch_force_cancels_when_agent_exits_without_terminal_status(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        # No intent_lookup entry → get_intent returns None → final_status = None.
        ps = FakePearscarf(intents=[{"intent_id": "i1", "owner": "hex", "body": "x"}])
        await w.tick(ps, _StuckRunner())
        await asyncio.gather(*w._running.values(), return_exceptions=True)

        # Both the claim and the force-cancel should have happened.
        assert ("i1", "in_progress") in ps.status_calls
        assert ("i1", "cancelled") in ps.status_calls

        events = [json.loads(line) for line in cfg.events.log_path.read_text().strip().split("\n")]
        failed = next(e for e in events if e["event"] == "intent_failed")
        assert "without terminal status" in failed["error"]

    asyncio.run(run())


def test_dispatch_force_cancels_when_agent_raises(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        ps = FakePearscarf(intents=[{"intent_id": "i1", "owner": "hex", "body": "x"}])
        await w.tick(ps, _RaisingRunner())
        await asyncio.gather(*w._running.values(), return_exceptions=True)

        assert ("i1", "cancelled") in ps.status_calls
        events = [json.loads(line) for line in cfg.events.log_path.read_text().strip().split("\n")]
        failed = next(e for e in events if e["event"] == "intent_failed")
        assert "boom" in failed["error"]

    asyncio.run(run())


def test_dispatch_force_cancels_when_agent_times_out(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        # Tight timeout via runtime_config so the test stays fast.
        ps = FakePearscarf(
            intents=[
                {
                    "intent_id": "i1",
                    "owner": "hex",
                    "body": "x",
                    "runtime_config": {"max_duration_seconds": 0},
                }
            ]
        )
        await w.tick(ps, _TimeoutRunner())
        await asyncio.gather(*w._running.values(), return_exceptions=True)

        assert ("i1", "cancelled") in ps.status_calls
        events = [json.loads(line) for line in cfg.events.log_path.read_text().strip().split("\n")]
        failed = next(e for e in events if e["event"] == "intent_failed")
        assert "timeout" in failed["error"]

    asyncio.run(run())


def test_dispatch_does_not_force_cancel_when_agent_set_done(tmp_path: Path) -> None:
    """When the agent properly flips status → done, the force-cancel path is skipped."""

    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        ps = FakePearscarf(
            intents=[{"intent_id": "i1", "owner": "hex", "body": "x"}],
            intent_lookup={"i1": {"intent_id": "i1", "status": "done"}},
        )
        await w.tick(ps, _StuckRunner())  # noop runner; pretend the agent set done
        await asyncio.gather(*w._running.values(), return_exceptions=True)

        # Only the claim should be present; no force-cancel.
        statuses = [s for _, s in ps.status_calls]
        assert "in_progress" in statuses
        assert "cancelled" not in statuses

    asyncio.run(run())


# --- coordinator wake mechanism ---


def test_wake_coordinator_wakes_when_no_children_and_past_debounce(tmp_path: Path) -> None:
    """A coordinator with zero in-flight children + past debounce → re-dispatch."""

    async def run() -> None:
        cfg = _config(tmp_path)
        cfg.orchestrator.coordinator_wake_debounce_seconds = 0  # no debounce
        w = Workestrator(cfg)
        coord = {
            "intent_id": "c1",
            "owner": "greg",
            "intent_type": "coordinator",
            "status": "in_progress",
            "body": "coord",
        }
        ps = FakePearscarf(intents=[coord])
        runner = FakeRunner()
        await w.tick(ps, runner)
        await asyncio.gather(*w._running.values(), return_exceptions=True)
        # Coordinator was woken (re-dispatched) — runner saw it.
        assert "c1" in runner.calls
        events = [json.loads(line) for line in cfg.events.log_path.read_text().strip().split("\n")]
        assert any(e["event"] == "coordinator_woken" for e in events)

    asyncio.run(run())


def test_wake_coordinator_skipped_when_children_in_flight(tmp_path: Path) -> None:
    """A coordinator with at least one in_progress / todo child must wait for
    the natural completion → no wake."""

    async def run() -> None:
        cfg = _config(tmp_path)
        cfg.orchestrator.coordinator_wake_debounce_seconds = 0
        w = Workestrator(cfg)
        coord = {
            "intent_id": "c1",
            "intent_type": "coordinator",
            "status": "in_progress",
            "body": "coord",
        }
        child = {
            "intent_id": "child1",
            "status": "in_progress",
            "parent_record_id": "c1",
            "body": "child",
        }
        ps = FakePearscarf(intents=[coord, child])
        runner = FakeRunner()
        await w.tick(ps, runner)
        await asyncio.gather(*w._running.values(), return_exceptions=True)
        # Coordinator NOT woken (child still in flight).
        assert "c1" not in runner.calls

    asyncio.run(run())


def test_wake_coordinator_skipped_when_within_debounce(tmp_path: Path) -> None:
    """A coordinator woken < debounce ago does not get woken again."""

    async def run() -> None:
        cfg = _config(tmp_path)
        cfg.orchestrator.coordinator_wake_debounce_seconds = 300
        w = Workestrator(cfg)
        # Simulate a recent wake by pre-populating _last_wake.
        w._last_wake["c1"] = time.monotonic()
        coord = {
            "intent_id": "c1",
            "intent_type": "coordinator",
            "status": "in_progress",
            "body": "coord",
        }
        ps = FakePearscarf(intents=[coord])
        runner = FakeRunner()
        await w.tick(ps, runner)
        await asyncio.gather(*w._running.values(), return_exceptions=True)
        assert "c1" not in runner.calls

    asyncio.run(run())


def test_wake_coordinator_skipped_when_already_running(tmp_path: Path) -> None:
    """A coordinator with an in-flight dispatch task is not re-dispatched."""

    async def run() -> None:
        cfg = _config(tmp_path)
        cfg.orchestrator.coordinator_wake_debounce_seconds = 0
        w = Workestrator(cfg)

        async def never() -> None:
            await asyncio.sleep(10)

        running_task = asyncio.create_task(never())
        w._running["c1"] = running_task
        try:
            coord = {
                "intent_id": "c1",
                "intent_type": "coordinator",
                "status": "in_progress",
                "body": "coord",
            }
            ps = FakePearscarf(intents=[coord])
            runner = FakeRunner()
            await w.tick(ps, runner)
            assert "c1" not in runner.calls
        finally:
            running_task.cancel()
            await asyncio.gather(running_task, return_exceptions=True)

    asyncio.run(run())


def test_dispatch_emits_coordinator_paused_on_normal_exit(tmp_path: Path) -> None:
    """A coordinator that exits cleanly without setting terminal status →
    emit coordinator_paused, leave in_progress (do NOT force-cancel)."""

    async def run() -> None:
        cfg = _config(tmp_path)
        cfg.orchestrator.coordinator_wake_debounce_seconds = 0
        w = Workestrator(cfg)
        coord = {
            "intent_id": "c1",
            "owner": "greg",
            "intent_type": "coordinator",
            "status": "in_progress",
            "body": "coord",
        }
        ps = FakePearscarf(intents=[coord])
        # Pre-bypass the wake path by marking last_wake as recent so tick doesn't
        # immediately re-dispatch the coordinator a second time after _dispatch
        # finishes inside this tick. We test the dispatch finally-block in
        # isolation by spawning it directly.
        w._last_wake["c1"] = time.monotonic()
        await w._dispatch(coord, _StuckRunner(), ps)

        # No force-cancel — coordinator-paused is the right outcome.
        assert "cancelled" not in [s for _, s in ps.status_calls]
        events = [json.loads(line) for line in cfg.events.log_path.read_text().strip().split("\n")]
        kinds = [e["event"] for e in events]
        assert "coordinator_paused" in kinds
        assert "intent_failed" not in kinds

    asyncio.run(run())


def test_dispatch_force_cancels_coordinator_on_timeout(tmp_path: Path) -> None:
    """Coordinator timeouts are still failures — force-cancel applies."""

    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        coord = {
            "intent_id": "c1",
            "intent_type": "coordinator",
            "status": "in_progress",
            "body": "coord",
            "runtime_config": {"max_duration_seconds": 0},
        }
        ps = FakePearscarf(intents=[coord])
        await w._dispatch(coord, _TimeoutRunner(), ps)
        assert ("c1", "cancelled") in ps.status_calls
        events = [json.loads(line) for line in cfg.events.log_path.read_text().strip().split("\n")]
        failed = next(e for e in events if e["event"] == "intent_failed")
        assert "timeout" in failed["error"]

    asyncio.run(run())


def test_dispatch_force_cancels_coordinator_on_raise(tmp_path: Path) -> None:
    """Coordinator crashes are still failures — force-cancel applies."""

    async def run() -> None:
        cfg = _config(tmp_path)
        w = Workestrator(cfg)
        coord = {
            "intent_id": "c1",
            "intent_type": "coordinator",
            "status": "in_progress",
            "body": "coord",
        }
        ps = FakePearscarf(intents=[coord])
        await w._dispatch(coord, _RaisingRunner(), ps)
        assert ("c1", "cancelled") in ps.status_calls

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
