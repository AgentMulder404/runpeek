# Cross-platform slice — what was actually tested (2026-09-11)

Machine: maintainer's macOS 25.1 (arm64), Python 3.12 venv for the suite,
`claude` 2.1.269, Codex CLI 0.154.0 via `npx`, `mcp` 2.2.0, PyInstaller 6.22.2.
Version under test: RunPeek 0.4.0a1 on branch `codex/cross-platform-slice`.

## Automated suite

`ruff check`, `mypy --strict` and `pytest` (180 tests) pass. New tests:

| File | Covers |
|---|---|
| `tests/test_telemetry.py` | parsing the **sanitised real** Claude Code and Codex OTLP payloads; allowlist (emails, account/org ids never stored); idempotent storage; Claude transcript ⟷ telemetry merge by `request_id` in both arrival orders; Codex transcript quarantine in both orders; receiver over real HTTP: 401 without/with wrong token, 415 protobuf, 400 malformed, 413 oversized, metrics/traces accepted and discarded; two agents on two tasks with interleaved arrival and re-delivery; unknown model and missing usage |
| `tests/test_connectors.py` | Claude Code `settings.json`: refuse existing exporter, connect, reconnect keeps original values, disconnect restores the original object; connect from no file; Codex `config.toml`: marked block appended, existing `[otel]`/`[mcp_servers.runpeek]` refused, disconnect restores bytes; Gemini detected only; "connected" vs "collecting" wording |
| `tests/test_mcp_tools.py` | tool bodies: create/list/attach `current` (only with `CLAUDE_CODE_SESSION_ID`), id validation, browser conversation attach as unmetered with keyed hash (id never stored), move between tasks, unsupported URL, health text; one real stdio round trip through the official `mcp` client against `runpeek mcp` |

Fixture provenance: `tests/fixtures/otel/*.jsonl` are the exact HTTP bodies
Claude Code and Codex sent to the probe receiver, with `user.email`,
`user.account_uuid`, `user.account_id`, `user.id`, `organization.id`,
`host.name` and `terminal.type` replaced. Token counts, ids, timestamps and
event names are unchanged.

## Live vertical slice on the developer machine (real agents, real accounts)

This is a **developer-machine** run, not a fresh-user test. Steps executed in
order, with the real `~/.claude/settings.json` and `~/.codex/config.toml`:

1. `runpeek agents connect claude-code --port 4327` and `… connect codex` —
   backups written, env block and marked block added, `claude mcp add` run.
2. `runpeek telemetry serve --port 4327` in the background; `/healthz` ok.
3. A real `claude -p` session (Haiku 4.5, `bypassPermissions`, tools limited to
   the three RunPeek tools) was asked to create a task, attach the current
   session and report. It returned the compact report: task `wi-af3f0a`,
   session attached from `CLAUDE_CODE_SESSION_ID`, 5 calls priced at that
   moment from the agent's own `cost_usd_micros`; 7 after the session finished.
4. A real `codex exec` run (gpt-6-astra, ChatGPT plan) picked up the
   `[otel]` block from `config.toml` at start and exported two
   `response.completed` events with the bearer header; its thread was
   attached with `runpeek work assign`.
5. A ChatGPT conversation URL (**synthetic uuid**, no real conversation) was
   attached with `runpeek work attach-conversation` as an unmetered participant.
6. `runpeek work show wi-af3f0a --trace 6`: accounted $0.2275, all provisional;
   9 calls priced (7 agent-reported, 2 rate card `openai-list@2026-09-10`);
   by agent, by model, by session, timeline, one unmetered participant, coverage
   complete, source events with `request_id` for Claude Code and the
   conversation-scoped usage id for Codex.
7. `runpeek agents status`: receiver running, both agents "collecting (last
   event …)", Gemini "unverified".
8. `runpeek agents disconnect claude-code` / `codex`: `settings.json` equal to
   the pre-connect object (re-serialised), `config.toml` byte-identical, the
   `runpeek` MCP registration gone from `claude mcp list`.

Not exercised live: Codex Desktop / VS Code (needs an app restart with the
block in place), the launchd service (installed only in the fresh-environment
flow below with `--no-service`, so **not** tested), Gemini CLI (no credentials).

## Fresh-environment installation test (isolated, not a fresh machine)

Environment: `env -i HOME=<temp> PATH=/usr/bin:/bin` on the same Mac — no
repository checkout, no `runpeek` package, no virtual environment, no `uv`,
no developer dependencies on `PATH`. The only Python involved is
`/usr/bin/python3`, used **as the test driver** (to POST fixture payloads and
speak JSON-RPC to the MCP server), never by RunPeek. Agent homes were pointed
at temp directories through `RUNPEEK_CLAUDE_HOME` / `RUNPEEK_CODEX_HOME`.

Artifact: `dist/bin/runpeek`, a 25 MB Mach-O arm64 onefile executable built
with `packaging/runpeek.spec`. Not signed, not notarised, not published.

Run A (no agents installed): `runpeek --version` → `runpeek 0.4.0a1`;
`runpeek setup --yes --no-service` correctly reports "No supported agent found
to connect"; `runpeek telemetry serve` accepted the three real Claude Code
payloads (200 each) with the store's bearer token; `runpeek mcp` over stdio
created a task, attached the session named by `CLAUDE_CODE_SESSION_ID`, and
reported `$0.019065` for the two replayed calls; `runpeek work list` shows it.

Run B (agents simulated as installed: `auth.json` for Codex and a **stand-in
`claude` script** that only records its arguments): `runpeek setup --yes
--no-service --no-import` connected both, wrote the env block and the marked
block with the store's token, invoked `claude mcp add -s user runpeek --
<binary> mcp`, and printed "connected, no usage received yet" for both.
`disconnect` restored `config.toml` byte-for-byte and `settings.json` to the
same JSON object (formatting differs: RunPeek re-serialises with two-space
indent).

What this does **not** prove: the `curl | sh` installer (`packaging/install.sh`
is written but there is no release to download from), Gatekeeper behaviour of
the unsigned binary on another Mac, Linux/Windows builds, launchd service
installation, and real `claude mcp add` behaviour from the binary (the live
slice above used the developer venv for that step).

## Failure modes exercised

| Scenario | Where | Result |
|---|---|---|
| Two agents on different tasks concurrently, interleaved delivery | test_telemetry | no cross-task attribution; re-delivery adds nothing |
| Parent/child attribution | test_work_items, test_unified (existing) | subagents follow the nearest explicit ancestor |
| Duplicate telemetry and transcript observations | test_telemetry | Claude: merged by request id; Codex: quarantined, visible |
| Streaming interruptions, retries, late events | partly: late transcript after telemetry and vice versa; Codex stale repeats (existing tests) | **retries/aborted API attempts from telemetry (`api_error`) are not yet recorded** |
| Offline restart and replay | test_codex_ingest, test_unified (existing) | totals unchanged |
| Counter resets and log rotation | existing adapter tests | handled/flagged |
| Unknown models and missing usage | test_telemetry | unpriced / zero-usage rows, never $0 by guess |
| Cross-tenant access and credential revocation | test_unified (hub, existing) | isolated; **not re-audited in this slice** |
| Sensitive-field rejection | test_telemetry, test_agent_ingest, test_codex_ingest | emails/ids/prompts absent from store, exports and reports |
| Deletion followed by replay | test_unified (ledger tombstones, existing) | not resurrected |
| Clean install/uninstall and configuration restoration | fresh-environment runs A/B, test_connectors | restored; JSON formatting not byte-preserved |
