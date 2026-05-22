# Changelog

## 0.11.3
- `AgentRunner.run` now raises on undispatchable intents instead of silently early-returning. An intent without `owner_role` (and without `owner`) raises `ValueError`; an intent whose role manifest can't be loaded (missing `soul.md` / `skills.md` under `<roles_dir>/<owner_role>/`) raises `FileNotFoundError`. The orchestrator's `_dispatch` already catches `Exception`, force-cancels via `set_intent_status('cancelled')`, and emits `intent_failed` — so the failure mode now surfaces honestly in the events stream instead of being mis-reported as a clean `coordinator_paused`.

## 0.11.2
- Fix role-directory resolution. `AgentRunner.resolve_role_key` was preferring `owner` (the agent's identity, e.g. `Greg`, `Linus`) over `owner_role` (the canonical role-path slug, e.g. `gtm`, `gtm/linkedin-prospecting`) when picking the directory under `roles_dir`. That meant a coordinator intent with `owner=Greg, owner_role=gtm` led to a lookup at `<roles_dir>/Greg/` which doesn't exist, and the intent was silently skipped at every poll. Now prefers `owner_role` and falls back to `owner` only when `owner_role` is missing. Companion cleanup: the role-not-found warning now mentions the actual files the loader looks for (`soul.md` + `skills.md`) instead of the stale `prompt.md` filename, and the module docstring matches the loader's real shape.

## 0.11.1
- Switch the orchestrator's pearscarf client + the Claude adapter's MCP-config-for-subprocess to the streamable-HTTP transport, matching pearscarf 1.40.1 which retired SSE. `workestrator/pearscarf_client.py` now imports `mcp.client.streamable_http.streamablehttp_client` (3-tuple return; the get-session-id callable is ignored), and the JSON the Claude adapter writes via `--mcp-config` declares `"type": "http"` for the pearscarf server instead of `"sse"`. Without this fix the daemon connected to the new `/mcp` URL with SSE semantics and got a 400 Bad Request on every poll, never reaching pearscarf.
- Fix the wake-loop's `query_intents` call. The orchestrator was passing `intent_type=` (the pearscarf-side schema name), but the client's signature uses `type=` (matching the MCP tool's actual kwarg). The mismatch raised `TypeError: query_intents() got an unexpected keyword argument 'intent_type'` on the first tick of every coordinator-aware run, crashing the daemon. Now passes `type="coordinator"`.

## 0.11.0
- Drop the `claude-agent-sdk` dependency. The Claude adapter has been on `claude --print` subprocess + stream-json parsing since 0.8.0; the SDK was unused as of 0.10.0 but still pulled in as a transitive install. Now removed from `pyproject.toml` and `uv.lock` ahead of the SDK's 2026-06-15 paid transition. Project description updated to reflect the runtime-agnostic orchestrator surface (Claude today via the adapter seam; codex/hermes/… can register as additional adapters).

## 0.10.0
- Coordinator wake mechanism. The polling tick now also inspects `in_progress` intents where `intent_type="coordinator"`: for each, count children currently in flight (status `todo`/`in_progress`); if zero AND the time since the last wake exceeds the debounce window, re-dispatch the coordinator as a fresh session. Greg-shaped agents now correctly fire → exit → wait → fire-again as their children complete. Debounce defaults to 300s (configurable via `orchestrator.coordinator_wake_debounce_seconds`) and covers child-intent registration latency. The dispatch finally-block now distinguishes coordinator vs executor exits: coordinators that return cleanly without flipping their own status emit `coordinator_paused` and stay `in_progress` (waiting for the next wake); executors and any timeout/crash continue to force-cancel as in 0.9.0. New `coordinator_woken` event surfaces every wake.

## 0.9.0
- Stuck-detection in the dispatch loop. Each dispatched session now has a wall-clock cap (default 3600s from `agent.default_max_duration_seconds`, override per-intent via `runtime_config.max_duration_seconds`); exceeding it terminates the agent task. When an agent exits without flipping its intent to a terminal status — whether via timeout, crash, or returning normally without setting `done`/`cancelled` — workestrator now force-flips the intent to `cancelled` via pearscarf so the next poll doesn't re-claim it, and emits `intent_failed` with a reason (`timeout after Ns` / `agent raised: …` / `agent returned without terminal status`). Closes the previous failure mode where a crashed agent's intent stayed stuck `in_progress` indefinitely.

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
