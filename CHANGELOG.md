# Changelog

## 0.8.0
- `ClaudeAdapter` now spawns `claude --print --output-format stream-json --verbose ...` as a subprocess instead of calling `claude-agent-sdk.query()`. Decouples workestrator from the SDK ahead of its 2026-06-15 paid transition; sessions inherit the operator's Claude Code subscription auth (OAuth via keychain). Per-intent dispatch knobs come from `intent.runtime_config`: `chrome_required` → `--chrome`, `model` → `--model`, `max_budget_usd` → `--max-budget-usd`. Pearscarf MCP wires via `--mcp-config` JSON. Stream-json events are parsed line-by-line; `assistant` events are normalized to the prior SDK message shape so the core's per-message event emitter (`agent_text`, `agent_tool_use`) stays unchanged.

## 0.7.0
- Introduce a runtime-adapter seam in the dispatcher. `AgentRunner` now resolves an adapter from `intent.runtime` (e.g. `"claude"`) and routes the session through `Adapter.dispatch(intent, workspace, system_prompt, user_message) -> AsyncIterator[dict]`. Pure refactor — no behavior change. The existing claude-agent-sdk call moves into `workestrator/adapters/claude.py` verbatim; follow-up commits replace its internals with `claude --print` subprocess + stream-json. Sets the structure for non-Claude runtimes (codex, hermes, …) to register here without touching core orchestrator logic.

## 0.6.0
- Personas are now loaded as `<role>/soul.md` + `<role>/skills.md` (both required), with an optional shared `<roles_dir>/_pearscarf.md` foundation prepended to every dispatched session. Splits persona identity from procedure and lifts shared PearScarf mechanics out of each role file. Replaces the previous single-file `prompt.md` shape.

## 0.5.0
- Every dispatched role prompt is now prefixed with a universal `SAFETY_RIDER` that carries operator-side rules conflicting with bundled Claude Code defaults — chiefly the prohibition on `Co-Authored-By:` commit trailers and the terse-header commit convention. Belt-and-suspenders against Claude Code defaults bleeding through into autonomous agent commits.

## 0.4.0
- Workestrator now emits per-message events (`agent_text`, `agent_tool_use`) for every assistant turn inside a dispatched session, alongside the existing daemon + intent lifecycle events. A consumer tailing `events.jsonl` sees agent text and tool calls in near-real-time — what each persona is doing as they do it — without needing to open the transcript file.

## 0.3.0
- Spawned Claude Agent SDK sessions now run with `permission_mode='bypassPermissions'` so the agent's bash, edit, and write tools work without interactive operator approval — the autonomous workforce posture requires it. Each session's full message stream is persisted to `<workspace>/transcript.jsonl` (NDJSON) so failures can be diagnosed after the fact.

## 0.2.0
- Workestrator now emits a structured event stream (NDJSON) covering daemon and intent lifecycle — `daemon_started`, `daemon_stopping`, `intent_dispatched`, `intent_completed`, `intent_failed`. Lets a Claude Code Monitor (or any other consumer) tail real-time progress without polling PearScarf. Path configurable via the new `events.log_path` config key (default `./.workforce/events.jsonl`).

## 0.1.0
- Initial release. Polls a PearScarf MCP for `todo` intents, gates dispatch on `depends_on` satisfaction, claims one intent per available slot, and spawns Claude Agent SDK sessions per claim. Configured via `workestrator.yaml`.
