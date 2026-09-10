# Coding-agent sources — capability check (2026-09-09)

What each tool exposes, where the evidence came from, and what the observer
can therefore honestly measure. "Verified" means inspected on this machine or
read in the tool's official documentation on the date above.

## Claude Code — supported (adapter `runpeek.agents.claude_code`, experimental)

Installed: `claude` 2.1.258. Transcript files inspected: 14 main + 66 subagent
files written by 2.1.202–2.1.257, structure only (keys, counts; no content).

| Capability | Available | Source | Per-event / cumulative / estimate | Missing |
|---|---|---|---|---|
| Session identity | yes: `sessionId` = file stem under `~/.claude/projects/<encoded cwd>/` | docs (hooks receive `session_id`, `transcript_path`); verified on disk | — | — |
| Turn identity | yes: `promptId` on user entries; `system/turn_duration` carries `durationMs` | verified on disk (undocumented) | per turn | subagent transcripts reuse the parent's `promptId` (namespaced by the adapter) |
| Tool executions | yes: `tool_use` blocks (id, name, input) in assistant entries; `tool_result` (tool_use_id, `is_error`) in user entries; timestamps on both | verified on disk; `tool_use_id` documented in hooks | per action | `is_error` absent on ~half of results (treated as success); Bash side effects invisible |
| Provider usage | yes: `message.usage` on assistant entries — `input_tokens`, `cache_creation_input_tokens` with `cache_creation.ephemeral_5m/1h_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `server_tool_use.{web_search,web_fetch}_requests` | verified on disk (undocumented) | **per request**; several entries share one `message.id` with identical usage → deduplicated by `message.id` | — |
| Model | yes: `message.model` | verified | per request | internal `<synthetic>` pseudo-model appears; reported unpriced |
| Source-reported cost | **no** in transcripts | docs: `cost_usd` exists only in the OpenTelemetry export (`claude_code.api_request`) | — | requires `CLAUDE_CODE_ENABLE_TELEMETRY=1` + an OTLP endpoint; not consumed |
| API-equivalent cost | calculated | rate card `anthropic-list@2026-09-09` (verified against platform.claude.com pricing) | estimate | subscription users: not a charge, quota or saving — labelled as such everywhere |
| Lifecycle hooks | yes: 30+ events incl. `PreToolUse`, `PostToolUse`, `SessionStart/End`; input has ids, `tool_input`, `tool_response` | docs | per event | **no usage or cost in hook payloads**; not required by this observer, not configured |
| Permissions / config | none needed: transcripts are the user's own files, read-only | — | — | — |
| Format stability | writer `version` on every entry; adapter gated on major.minor `2.1` | verified | — | other versions parsed best-effort and marked `unknown_version` |

## Codex — not yet supported (interface designed, adapter not built)

Installed: no `codex` CLI on PATH; `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
present from the VS Code extension (37 files; `session_meta.cli_version`
0.130–0.153). Public documentation for session files, hooks or usage could not
be located (developers.openai.com/codex redirects; configuration pages 404).

| Capability | Available | Source | Per-event / cumulative / estimate | Missing |
|---|---|---|---|---|
| Session identity | yes: `session_meta.payload.{session_id,id,cwd,cli_version,source,originator}` | verified on disk (undocumented) | — | — |
| Turn identity | partial: `event_msg` `task_started` / `task_complete` / `turn_aborted` | verified on disk | per task | no stable prompt id observed |
| Tool executions | yes: `response_item` `function_call` / `function_call_output` (`call_id`, `name`), `custom_tool_call(_output)`, `web_search_call` | verified on disk | per call | error flag not observed at the top level; output is content |
| Provider usage | yes: `event_msg` `token_count` with `info.last_token_usage` (per turn) **and** `info.total_token_usage` (**cumulative**); fields `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens` | verified on disk | per turn + cumulative snapshot — an adapter must use `last_token_usage` or diff consecutive totals, never sum snapshots | model not on the event (session-level `model` in config) |
| Source-reported cost | not observed | — | — | — |
| Hooks | undocumented / not found | — | — | — |

Design path for the Codex adapter: same `events.py` vocabulary; `UsageEvent`
with `usage_kind="cumulative_snapshot"` for `total_token_usage` and
`per_request` for `last_token_usage`, keyed by `(session_id, ordinal)`; the
ingestor already refuses to sum snapshots. Building it is gated on either a
documented interface or a second structural inspection against a pinned
`cli_version`.

## What the observer refuses to claim, for both

- That a repeated file read was re-billed as input (not observable).
- Token or dollar *savings* from any finding.
- That silence, a permission wait, or user thinking is a stalled agent.
- That an API-equivalent figure is what a subscription user paid.
- Capture coverage: sessions written elsewhere than the documented location, or
  by other tools, are invisible and the summary says so.
