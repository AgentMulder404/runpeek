# Coding-agent sources — capability check (2026-09-10)

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

## Codex — supported (adapter `runpeek.agents.codex`, experimental)

Inspected 2026-09-10: 38 rollout files under `~/.codex/sessions/YYYY/MM/DD/`
written by Codex CLI 0.130.0-alpha.5 … 0.153.1 (Codex Desktop / VS Code),
structure only. No public documentation of the rollout format was found, so
every row is "verified on disk". Three sanitised real files are test fixtures
(`tests/fixtures/codex/`, produced by `sanitize.py` there).

| Capability | Available | Source | Per-event / cumulative / estimate | Missing |
|---|---|---|---|---|
| Session identity | yes: the file name's uuid (`rollout-<ts>-<uuid>.jsonl`). `session_meta.session_id` is the **root** session for subagent files, so it is not used as identity | verified on disk | — | — |
| Project | `session_meta.cwd`; `git.branch` (and `repository_url` on some versions) | verified | — | commit hash not stored |
| Subagents | 0.153+: `thread_source: subagent`, `parent_thread_id`, `forked_from_id`, `subagent_history_start_ordinal`; the parent records `SubAgentActivity` items | verified (2 subagent files) | — | the child file starts with a copy of the parent's history; usage below the start ordinal is skipped (none was observed there) |
| Turn identity | `event_msg` `task_started` (`turn_id`, `started_at`), `task_complete`, `turn_aborted` (`duration_ms`); `turn_context` names the turn's `model` | verified | per turn | `task_complete` has no duration: derived from `started_at` |
| Tool executions | `response_item` `custom_tool_call` (`exec`, `apply_patch`) and `function_call` (`exec_command`, `view_image`, `spawn_agent`, …) keyed by `call_id`; outputs in `*_output`; `item_completed` `McpToolCall` (status/error) and `WebSearch` | verified | per call | error flag only when the output carries an exit code or the MCP item failed; otherwise NULL |
| Provider usage | `event_msg` `token_count` with `info.total_token_usage` (**cumulative**) and `info.last_token_usage`; fields `input_tokens` (includes cached), `cached_input_tokens`, `cache_write_input_tokens` (always 0 observed), `output_tokens` (includes reasoning), `reasoning_output_tokens` | verified on 3,847 events | **one usage row per increase of the cumulative total**. `last_token_usage` repeated the previous value in 112 events (after `turn_aborted`) and disagreed with the total delta in 1; totals never decreased | model comes from the last `turn_context` / `thread_settings_applied`; 12 events preceded any (unpriced, counted) |
| Response ids | 0.153+: `token_usage_record.response_id` | verified | attached to the next usage row | absent on older versions |
| Source-reported cost | **no** (rate-limit percentages only) | — | — | — |
| API-equivalent cost | calculated | rate cards `openai-list@…` | estimate | subscription users: not a charge |
| Format stability | `cli_version` in `session_meta`; adapter gated on `0.130`–`0.153` | verified | — | other versions parsed best-effort and marked `unknown_version` |

Parent/child independence was checked, not assumed: the first `token_count`
in a subagent file has `total == last`, i.e. the child's counter starts at
zero and the parent's totals do not include it.

## What the observer refuses to claim, for both

- That a repeated file read was re-billed as input (not observable).
- Token or dollar *savings* from any finding.
- That silence, a permission wait, or user thinking is a stalled agent.
- That a resumed or forked transcript's replayed usage is new usage: it is
  counted once, under the first session that recorded it, and reported.
- That an API-equivalent figure is what a subscription user paid.
- Capture coverage: sessions written elsewhere than the documented location, or
  by other tools, are invisible and the summary says so.
