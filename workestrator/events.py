"""Structured event emitter for the workestrator daemon.

Writes one JSON object per line (NDJSON) to a configurable path. Consumed by
Claude Code Monitors, dashboards, or anything else that wants real-time
daemon state without polling PearScarf.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


class EventEmitter:
    """Append-only JSONL writer. Safe to call from concurrent asyncio tasks."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = Lock()

    def emit(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {"ts": _now_iso(), "event": event}
        record.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
