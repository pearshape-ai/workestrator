"""Adapter protocol — workestrator's runtime-agnostic dispatch interface.

An adapter spawns + runs one agent session for one dispatched intent. The
adapter handles everything runtime-specific (subprocess spawn, message
stream parsing); workestrator core handles everything runtime-agnostic
(intent lifecycle, transcript, event emission, future timeout / wake
mechanisms).

Each yielded dict is a message in the runtime's format normalized to a
JSON-friendly shape. Today that matches Claude Agent SDK's
`model_dump(mode="json")` output; future adapters translate to the same
shape so workestrator's per-message event emission stays uniform across
runtimes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol


class Adapter(Protocol):
    """Async iterator dispatch for a single intent.

    Implementations:
      - Receive the full intent dict (so adapters can self-select from
        `runtime_config` and other fields).
      - Receive the workspace path (cwd for the spawned session).
      - Receive the pre-assembled `system_prompt` and `user_message` from
        core (core handles role/persona loading; adapters handle agent
        spawn).
      - Yield message dicts as the session progresses.
      - Complete the iterator when the session terminates.
    """

    def dispatch(
        self,
        intent: dict[str, Any],
        workspace: Path,
        system_prompt: str,
        user_message: str,
    ) -> AsyncIterator[dict[str, Any]]: ...
