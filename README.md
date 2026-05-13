# Workestrator

A pearscarf-driven Claude orchestrator. Reads intents from a pearscarf MCP server, dispatches Claude agents to do the work, watches for completion. Stays out of the way.

Built as the orchestration layer underneath [pearscarf](https://github.com/pearshape-ai/pearscarf)-based AI workforce setups. Currently coupled to pearscarf (as the intent source) and Claude (via [`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python)) — both may become pluggable in future versions.

## How it works

```
pearscarf intents (status=todo, deps done) → workestrator polls →
role match → Claude session spawned → agent does work →
intent.status = done → next intent
```

The orchestrator owns one loop:

1. Poll pearscarf for `status="todo"` intents.
2. Filter to those whose `depends_on` are all `done`.
3. Match each to a role (by `owner`, falling back to `owner_role`).
4. Spawn a Claude session per eligible intent (up to `max_concurrent_agents`):
   - System prompt = `<roles_dir>/<role>/prompt.md`
   - User message = the intent body
   - MCP attached = pearscarf (so the agent can `query_facts`, `submit_record`, `set_intent_status`, etc.)
5. Wait for the session to exit. Move on.

The brain is pearscarf. The orchestrator is dumb on purpose — it just dispatches.

## Install

Not on PyPI. Install straight from GitHub:

```bash
uv add git+https://github.com/pearshape-ai/workestrator.git
# or
pip install git+https://github.com/pearshape-ai/workestrator.git
```

## Quick start

```bash
# 1. Copy the example config
cp workestrator.example.yaml workestrator.yaml

# 2. Set env vars
export PEARSCARF_MCP_URL=http://localhost:8090/sse
export ANTHROPIC_API_KEY=...                   # required by claude-agent-sdk
# export PEARSCARF_API_KEY=...                 # only if your pearscarf MCP requires auth

# 3. Make sure roles/<role>/prompt.md files exist (one per role you'll dispatch to)

# 4. Run
workestrator run --config workestrator.yaml
```

## Config

See `workestrator.example.yaml`. Defaults:

| Key | Default | Purpose |
|---|---|---|
| `pearscarf.mcp_url` | (required) | SSE endpoint of the pearscarf MCP server |
| `pearscarf.api_key` | `null` | Bearer token; omit if MCP is unauthenticated |
| `orchestrator.poll_interval_seconds` | `30` | How often to scan pearscarf for new work |
| `orchestrator.max_concurrent_agents` | `4` | Parallel Claude sessions cap |
| `roles.dir` | `./roles` | Where to look up `<role>/prompt.md` |
| `agent.model` | `claude-sonnet-4-5` | Model the spawned agents use |
| `agent.max_turns` | `50` | Per-session turn cap |
| `workspace.dir` | `./.workestrator/workspaces` | Per-intent working directories |
| `events.log_path` | `./.workforce/events.jsonl` | NDJSON event stream (daemon + intent lifecycle) |

`${VAR}` and `$VAR` references in string values are expanded from the process environment at load time.

## Library use

```python
import asyncio
from pathlib import Path

from workestrator import Workestrator
from workestrator.config import load

cfg = load(Path("workestrator.yaml"))
asyncio.run(Workestrator(cfg).run())
```

## Roles

The orchestrator doesn't know about specific personas — only that each intent has an `owner` (e.g. `hex`) and / or `owner_role` (e.g. `head-eng`), and that the role's prompt lives at `<roles_dir>/<role>/prompt.md`. Drop a per-role prompt in that location; the orchestrator loads it as the system prompt for that intent's session.

## Event stream

Alongside the regular log, workestrator emits an append-only NDJSON event stream at the path configured by `events.log_path` (default `./.workforce/events.jsonl`). One JSON object per line:

| `event` | When | Fields |
|---|---|---|
| `daemon_started` | After connecting to the pearscarf MCP | `poll_interval_seconds`, `max_concurrent_agents` |
| `intent_dispatched` | Claimed an intent and spawned its session | `intent_id`, `role`, `owner`, `title` |
| `intent_completed` | Session finished and the intent reached `done` or `cancelled` | `intent_id`, `role`, `owner`, `title`, `final_status` |
| `intent_failed` | Session raised, or returned without flipping status to a terminal state | `intent_id`, `role`, `owner`, `title`, `error` |
| `daemon_stopping` | Orchestrator received `CancelledError` (e.g. SIGTERM) | `in_flight` |

Tail this file from a Claude Code Monitor, a dashboard, or any other consumer that wants real-time daemon state without polling pearscarf.

## What it does *not* do

- No retry policy beyond "next poll picks it up again if status is still `todo`".
- No agent-level observability beyond the event stream + log lines.
- No multi-tenant safety. Trust your agents.
- No PyPI release. Install from git.

These can be added once their absence stings. The point is to stay small and clear while we learn how the loop actually behaves.

## License

MIT.
