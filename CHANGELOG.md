# Changelog

All notable changes to RunPeek are recorded here. The project is pre-release;
versions below 1.0 may change interfaces between minor versions.

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
