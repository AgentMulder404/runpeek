# Evidence matrix — what each platform actually exposes

Phase 0 of `CROSS_PLATFORM_ENGINEERING_PLAN.md`. Every row says how it was
verified. "Real session" means a live run on the maintainer's machine on
2026-09-11 with the exact client version shown; the captured payloads, with
identity fields replaced, are the fixtures under `tests/fixtures/otel/`.
"Docs" means read in the vendor's documentation on that date and **not**
exercised. Nothing below is inferred.

## Local coding agents

| | Claude Code | Codex | Gemini CLI |
|---|---|---|---|
| Client / version tested | `claude` 2.1.269 (CLI, `-p` mode; MCP transport `stdio`) | `@openai/codex` 0.154.0 CLI via `npx`, `codex exec`; Codex Desktop present but not driven | `@google/gemini-cli` 0.59.0 runs via `npx`; **no session run: no Google credentials on this machine** |
| Account type | Claude subscription (`user.account_uuid` present) | ChatGPT plan (`auth_mode: Chatgpt`) | none |
| Telemetry mechanism | OpenTelemetry logs, opt-in via env (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_LOGS_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`, endpoint, headers). Documented. | OpenTelemetry logs via `[otel]` in `config.toml` or `-c` overrides; `otlp-http` with `protocol = "json"` verified. Documented; applies to CLI, Desktop and VS Code per docs (Desktop not verified). | OpenTelemetry via `settings.json` `telemetry.*` or `GEMINI_TELEMETRY_*` env; `otlpProtocol` `http`/`grpc`; local `outfile`. Docs only. |
| Installation path used by RunPeek | `~/.claude/settings.json` → `env` block (reversible, backed up, existing exporter refused) + `claude mcp add -s user runpeek` | append a marked block to `~/.codex/config.toml` (`[otel]`, `[mcp_servers.runpeek]`); restart required; existing `[otel]` refused. `codex mcp add` is avoided: it rewrites and reorders the whole file (observed). | not configured (unverified) |
| Session identity | `session.id` on every event = transcript file stem. MCP servers spawned by Claude Code receive `CLAUDE_CODE_SESSION_ID` (+ `CLAUDE_PROJECT_DIR`) in their environment — verified twice. | `conversation.id` on every event = rollout file thread id (`thread.started` in `--json`). MCP servers receive **no** identity variables (verified: empty env snapshot). | `session.id`, `prompt_id` (docs) |
| Request identity | `request_id` (`req_…`) on `api_request`; the transcript's `requestId` is the same value → exact cross-source match | none exported. Rollouts carry `token_usage_record.response_id` (0.153+) but telemetry does not → no exact match possible | none documented at request level |
| Token fields (real event) | `input_tokens` 10, `output_tokens` 33, `cache_read_tokens` 13607, `cache_creation_tokens` 8295, `model` `claude-haiku-4-5-20251001`, `query_source` (`sdk`, `generate_session_title`, `compact`…) | `codex.sse_event` `event.kind=response.completed`: `input_token_count` 18215 (**includes** cached), `cached_token_count` 12928, `output_token_count` 5, `reasoning_token_count` 0, `cache_write_token_count` 0, `tool_token_count` 18220 (total). A pre-warm `response.completed` with 13079 input / 0 output was exported that the rollout did **not** count. | `gemini_cli.api_response`: `input_token_count`, `output_token_count`, `cached_content_token_count`, `thoughts_token_count`, `tool_token_count` (docs) |
| Pricing basis | `cost_usd` / `cost_usd_micros` exported by the agent (939 micros for 899 in + 8 out on Haiku 4.5, equal to the list-price rate card). Treated as **agent-reported estimate**, never as a bill: subscription sessions carry it too. | none; RunPeek rate cards (list prices, dated) | none; rate card would be needed |
| Sensitive fields present by default | `user.email`, `user.account_uuid`, `user.account_id`, `organization.id`, `user.id`, `terminal.type` on every event. Prompts/responses are `<REDACTED>` unless `OTEL_LOG_USER_PROMPTS=1`. | `user.email`, `user.account_id`, `host.name`, `mcp_servers` list. Prompt `[REDACTED]` unless `log_user_prompt = true`. | `user.email`, `installation.id`; `logPrompts` defaults **true** (docs) |
| RunPeek handling | allowlist: model, tokens, cost, request/session/prompt ids, query source, service version. Everything else is never read into the store. Metrics and traces are accepted and discarded. | same allowlist; no cost field | n/a |
| Transcript adapter (backfill) | `~/.claude/projects/**/*.jsonl`, keyed by `message.id`; merges with telemetry through `request_id` | `~/.codex/sessions/**/rollout-*.jsonl`, deltas of cumulative totals; **quarantined** for sessions that telemetry has observed (no shared id) | none |
| Permissions needed | none beyond the user's own files; env change applies to new sessions only | none; config change applies at next start | — |
| Limitations | OTLP export is per session process; nothing is exported for sessions started before connecting. `OTEL_*` variables are stripped from subprocesses, so hooks/MCP servers cannot see them (docs). | telemetry omits the request id; Desktop and VS Code paths untested; `codex exec` refuses MCP tool calls without an approval policy | unverified end to end |

## Browser conversations

| | ChatGPT | Claude.ai |
|---|---|---|
| Custom remote MCP connector | Streamable HTTP + OAuth 2.1; account/plan availability not verified | Available on Free (1 connector), Pro, Max, Team, Enterprise; server must be reachable from Anthropic IP ranges; OAuth |
| Conversation identity for a server | `_meta["openai/session"]` "anonymized conversation id for correlating tool calls within the same ChatGPT session"; `openai/subject` anonymized user id (Apps SDK reference, docs) | none documented |
| Native token usage available to a connector | no | no |
| Official usage export | not investigated beyond docs: no per-conversation token export is documented for consumer plans | none documented for consumer plans |
| RunPeek today | conversations attach to a task **by URL** as unmetered participants; the id is stored only as a keyed hash | same |
| Not done | remote MCP server deployment, OAuth, browser extension | same |

Binding through a remote connector is therefore feasible for ChatGPT with
`openai/session` as the key and needs an explicit code/link for Claude.ai.
Neither yields token accounting; the report shows these participants as
unmetered, and no visible-text estimate is offered.

## Interaction surface

| | Verified |
|---|---|
| MCP Python SDK | `mcp` 2.2.0 (`MCPServer`, `run(transport="stdio")`); tool round trip tested with the SDK's own client |
| Claude Code MCP registration | `claude mcp add -s user runpeek -- <runpeek> mcp` writes `~/.claude.json` (used by the connector; also removed on disconnect) |
| Codex MCP registration | `[mcp_servers.runpeek]` in `config.toml`; tool calls in `codex exec` require an approval policy (observed: "MCP tool call requires approval, but approval policy is never") |
| Token cost of the interface | not zero: tool schemas and replies are kept short (six tools, replies under 30 lines); no background model calls anywhere in RunPeek |
| Packaging | PyInstaller 6.22.2 onefile binary builds on macOS arm64; MCPB 2.1.2 supports a `uv` server type (no user Python) and `binary` type with `platform_overrides` — bundle not built yet |

## Dependencies recorded, not resolved

- Gemini CLI real session: needs a Google account or `GEMINI_API_KEY` on the test machine.
- Codex Desktop and VS Code telemetry: needs the desktop app restarted with the connector block in place and a real conversation.
- ChatGPT/Claude remote connector: needs a public HTTPS host and OAuth; not deployable from this repository yet.
- Billing exports (OpenAI usage CSV, Anthropic console): not investigated in this phase.
