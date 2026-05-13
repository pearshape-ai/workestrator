"""Per-intent workspace directories.

A workspace is just a filesystem directory the orchestrator hands an agent
to work in. Each intent gets its own subdir under the configured root so
two concurrent agents never collide on cwd.
"""

from __future__ import annotations

import re
from pathlib import Path


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def for_intent(self, intent_id: str) -> Path:
        key = _sanitize(intent_id)
        path = self.root / key
        path.mkdir(parents=True, exist_ok=True)
        return path


def _sanitize(intent_id: str) -> str:
    """Replace anything not in [A-Za-z0-9._-] with '_' for safe directory names."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", intent_id)
