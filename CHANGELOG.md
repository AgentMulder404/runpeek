# Changelog

All notable changes to RunPeek are recorded here. The project is pre-release;
versions below 1.0 may change interfaces between minor versions.

## 0.3.0a1 — shared accounting private pilot

- Local init/collect/activate/join flows, metadata-only ledger, explicit actual/estimated/
  allocated cost reconciliation, custom orchestration SDK, bounded queue and zero model-call tracking.
- Optional TLS receiver with workspace-scoped device credentials, operator-approved pairing,
  OS credential storage, revocation, input limits, quotas, replay protection and sync backoff.
- Local and hub export/deletion, replay tombstones and explicit attribution corrections.
- Repair legacy unstable IDs with backup and preservation of work assignments; fail closed
  on ambiguous no-ordinal subagent histories. Recursive listings, full trace IDs, correct
  inherited counts and zero-cost shares. Ancestry is resolved in memory.
- See docs/UNIFIED.md for setup, recovery and the private-pilot deployment boundary.

## 0.2.0a1 — 2026-09-10 — work items, Codex, trustworthy cross-agent accounting

Coding-agent accounting is now the primary product. The SDK harness
(`runpeek run`) is unchanged and still supported.

**Work items** (`runpeek work …`)
- Persistent work items — task, feature, bugfix, deployment — with a stable
  id, name, repository, status (open/closed), outcome (completed, incomplete,
  failed, abandoned) and optional issue, branch, PR and deployment references.
- Explicit assignment of sessions (`assign`, `unassign`, reassignment is
  recorded). A session belongs to at most one item; subagent sessions follow
  their parent unless assigned elsewhere. Nothing is assigned automatically:
  `suggest` lists unassigned sessions that match the repository or branch.
- `work show`: estimated model cost labelled as API-equivalent at list prices,
  breakdown by agent, model and session, a spending timeline, outcome,
  unpriced activity and missing usage as a priced subtotal with coverage,
  pricing provenance (rate card, resolution, calculation version) and
  `--trace` for the source usage records with their ids. `--pin` re-prices one
  provider's calls under a chosen card for that view only.
- `sessions` shows the agent and work item per session; `--unassigned`.

**Codex adapter** (`runpeek.agents.codex`, experimental) — built against 38
real rollout files from Codex CLI 0.130–0.153 on the maintainer's machine;
three sanitised real records ship as test fixtures.
- Usage is the *difference of consecutive cumulative totals*, never a sum of
  `last_token_usage` (112 of 3,847 inspected events repeated the previous value
  after `turn_aborted`). Repeats are counted and reported, never priced.
- Subagent threads (`thread_source: subagent`) are linked to their parent by
  the file's thread id; the copied history prefix is skipped; totals were
  verified to restart at zero, so parent and child usage are independent.
- Token categories normalised: `input_tokens` is uncached input; cached and
  reasoning tokens are kept separately. `token_usage_record` (0.153+) supplies
  the response id only.
- Tool calls from `function_call` / `custom_tool_call`, MCP calls and web
  searches from completed items; exit codes become error flags where present,
  otherwise NULL (not "success").

**Accounting**
- A usage record seen again under another session (resumed or forked
  transcript; 268 such Claude Code message ids found on the maintainer's
  machine) is counted once under the first session and recorded in
  `agent_usage_duplicates`; sessions and reports say what was shared.
- Resuming ingestion mid-file replays the consumed prefix so stateful
  adapters (cumulative totals, current turn, model) continue correctly.
- Rate card `openai-list@2026-09-10` adds `gpt-5.5` (verified 2026-09-10 at
  developers.openai.com/api/docs/pricing); every other price re-verified equal.
  Anthropic prices re-verified equal on 2026-09-10. Usage before a model's
  verification date stays unpriced under the effective-at-execution rule.
- Tool results without an observed error flag are stored as NULL, not 0.

**Schema** (forward-only, applied on open): new columns on `agent_sessions`
(`provider`, `git_branch`, `repository_url`, `usage_duplicates`,
`duplicate_of_session_id`, `usage_consistency`) and `agent_usage`
(`provider`, `reasoning_tokens`, `ordinal`); new tables
`agent_usage_duplicates`, `work_items`, `work_item_sessions`,
`work_item_events`. All exported by `runpeek export`.

**CLI wording**: `watch` defaults to `--source all` (Claude Code and Codex);
the sessions list is "RECENT CODING-AGENT SESSIONS".


## 0.1.0a1 — 2026-09-10 — first public pre-release (PyPI, GitHub)

RunPeek is an early, local-first observability harness. It is **not** stable
or production-proven; interfaces may change before 1.0.

**Supported and tested**
- `runpeek run`: OpenAI Python SDK `chat.completions.create`, synchronous,
  non-streaming (`openai==2.44.0`), with exactly-once invocation and
  fail-open hooks; `runpeek.job()` attribution; plain summary and
  `--verbose` accounting ledger; JSONL export; dated rate cards with
  idempotent repricing under pinned perspectives.
- `runpeek watch`: Claude Code sessions — **experimental** transcript
  adapter built against writer versions 2.1.202–2.1.257 (CLI 2.1.258);
  historical catch-up summarised, live event feed, three deterministic
  diagnostics (repeated failure, repeated read, retry loop), `sessions`,
  `session`, `findings`.

**Known measurement limits**
- Estimated cost is a list-price calculation from source-reported tokens: not
  a subscription charge, quota, reconciled bill, or saving. Unknown models,
  errors and streaming calls stay unpriced/unknown. Capture coverage is not
  measurable. Repeated reads are not proof of repeated billing; findings are
  potential inefficiencies, not waste. Mocked tests exercise accounting, not
  provider billing.

**Migration from nemulai** — see below and `docs/MIGRATION.md`.

**Unfinished verification** — the real-provider smoke test has not been run
by the maintainers; live incremental ingestion was verified only against the
maintainer's own Claude Code session (see the release report), not across
other environments.

### Rebrand: NemulAI harness → RunPeek by NemulAI

- Distribution `runpeek`, import package `runpeek`, CLI `runpeek`.
- Environment variables are `RUNPEEK_*`. The legacy `NEMULAI_*` names are
  still honoured (`RUNPEEK_*` wins when both are set) and will be removed in a
  later release.
- Default store moved from `./.nemulai/nemulai.db` to `./.runpeek/runpeek.db`.
  An existing legacy store is used in place with a one-line notice until you
  move it; it is never copied, overwritten or abandoned. See `docs/MIGRATION.md`.
- Attribution carriers (`runpeek.inject()`) now use `runpeek-*` keys;
  `extract()` accepts the legacy `nemulai-*` keys.
- **Intentional break:** there is no `nemulai` import package or CLI shim. The
  `nemulai` name on PyPI belongs to a different project (a GPU telemetry agent)
  and RunPeek must not collide with it. Replace `import nemulai` with
  `import runpeek` and `nemulai …` with `runpeek …`.

### Added

- `runpeek watch`: local observer for Claude Code sessions with a readable
  startup block, historical catch-up (summarised, not replayed) and an
  event feed (new session, turn start/finish, potential inefficiencies,
  collection problems). `runpeek sessions`, `runpeek session <id>`,
  `runpeek findings`.
- Three deterministic diagnostics: repeated failure, repeated read, retry loop
  (repeated tool errors / tight loop). Items are consolidated per evidence run
  and updated in place.
- Plain `runpeek run` / `runpeek summary` view; full accounting under `--verbose`.
- Dated rate cards `openai-list@2026-09-09` and `anthropic-list@2026-09-09`,
  verified against the providers' pricing pages on that date.
- Terminal policy: `NO_COLOR`, non-TTY and `--no-color` disable colour; output
  wraps to the terminal width; external strings are sanitised.
- CI workflow, issue forms, PR template, CONTRIBUTING, SECURITY.

### Milestone M1 (previously shipped as the NemulAI harness)

- Observes the OpenAI Python SDK's synchronous, non-streaming
  `chat.completions.create` with exactly-once invocation and fail-open hooks.
- Operations, attempts, identifiers by kind, source observations, exact
  correlation, potential charges with billing and estimation status,
  immutable rate cards, idempotent estimate revisions, pinned perspectives.
- Bounded queue, background writer, bounded shutdown, honest loss counters.
