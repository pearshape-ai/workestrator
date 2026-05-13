# Changelog

## 0.2.0
- Workestrator now emits a structured event stream (NDJSON) covering daemon and intent lifecycle — `daemon_started`, `daemon_stopping`, `intent_dispatched`, `intent_completed`, `intent_failed`. Lets a Claude Code Monitor (or any other consumer) tail real-time progress without polling PearScarf. Path configurable via the new `events.log_path` config key (default `./.workforce/events.jsonl`).

## 0.1.0
- Initial release. Polls a PearScarf MCP for `todo` intents, gates dispatch on `depends_on` satisfaction, claims one intent per available slot, and spawns Claude Agent SDK sessions per claim. Configured via `workestrator.yaml`.
