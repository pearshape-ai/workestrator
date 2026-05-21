"""Runtime adapter package — dispatch surfaces per agent runtime.

Workestrator core selects the adapter by `intent.runtime` (e.g. `"claude"`,
later `"codex"`, `"hermes"`, …). Adapters live as one module per runtime;
this package's re-exports are what `AgentRunner` registers in its dict
literal at init.
"""

from workestrator.adapters.base import Adapter
from workestrator.adapters.claude import ClaudeAdapter

__all__ = ["Adapter", "ClaudeAdapter"]
