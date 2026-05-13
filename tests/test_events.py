"""Tests for the EventEmitter."""

from __future__ import annotations

import json
from pathlib import Path

from workestrator.events import EventEmitter


def test_emit_writes_one_json_per_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    e = EventEmitter(path)
    e.emit("intent_dispatched", intent_id="i1", role="head-eng", owner="hex")
    e.emit("intent_completed", intent_id="i1", final_status="done")
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    assert a["event"] == "intent_dispatched"
    assert a["intent_id"] == "i1"
    assert a["role"] == "head-eng"
    assert "ts" in a
    assert b["event"] == "intent_completed"
    assert b["final_status"] == "done"


def test_emit_drops_none_valued_fields(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    e = EventEmitter(path)
    e.emit("intent_dispatched", intent_id="i1", role=None, owner="hex", title=None)
    record = json.loads(path.read_text().strip())
    assert "role" not in record
    assert "title" not in record
    assert record["owner"] == "hex"


def test_emit_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "events.jsonl"
    e = EventEmitter(path)
    e.emit("daemon_started")
    assert path.exists()
    record = json.loads(path.read_text().strip())
    assert record["event"] == "daemon_started"
