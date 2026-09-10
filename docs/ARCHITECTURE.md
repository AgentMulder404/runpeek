# Architecture

```
 ┌─────────────────────────── your machine ───────────────────────────┐
 │                                                                     │
 │  Python app ──► openai SDK ──► provider        Claude Code ──► ~/.claude/projects/<proj>/*.jsonl
 │       │  (runpeek run patches chat.completions.create;                          │
 │       │   exactly-once call, fail-open hooks)                                   │ runpeek watch
 │       ▼                                                                         ▼  (read-only polling)
 │  operations · attempts · observations                       sessions · turns · tool calls · usage
 │       │                                                                         │
 │       └──────────────► SQLite  ./.runpeek/runpeek.db  ◄──────────────────────────┘
 │                          │  accounting pass: charges → rate cards → estimates (perspectives)
 │                          │  diagnostics: repeated failure · repeated read · retry loop
 │                          ▼
 │        runpeek summary · events · export        runpeek sessions · session · findings
 └─────────────────────────────────────────────────────────────────────┘
```

Two sources, one store, one vocabulary. The harness measures *your* code's
provider calls; the watcher reads *Claude Code's* own transcripts. They are
never summed into one total.

## Packages

| Module | Role |
|---|---|
| `runpeek.context` | `job()` attribution via `ContextVar`; `inject`/`extract` carriers; `wrap` for threads |
| `runpeek.instrumentation.openai_chat` | the one adapter: class-level patch of `Completions.create`, sync, non-streaming |
| `runpeek.store` | SQLite (WAL), bounded queue, background writer, bounded shutdown, loss counters |
| `runpeek.accounting` | charges, selection, exact correlation, idempotent estimate revisions |
| `runpeek.rates` / `runpeek.perspective` | dated rate cards, resolution, perspectives |
| `runpeek.summary` | plain and ledger (`--verbose`) views of a run |
| `runpeek.agents.claude_code` | experimental transcript adapter (writer version gated) |
| `runpeek.agents.ingest` | checkpointed, idempotent ingestion; partial lines, truncation, rotation |
| `runpeek.agents.diagnostics` | deterministic detectors; one consolidated item per evidence run |
| `runpeek.agents.watch` / `report` | the live feed and review surfaces |
| `runpeek.ui` | colour policy, width, wrapping, sanitisation |
| `runpeek.cli` | `runpeek …` |

## Invariants the tests hold

- The wrapped SDK method runs exactly once; its result or exception passes through.
- Unknown usage, unknown prices and unknown billing stay unknown — never zero.
- One current estimate per (charge, perspective); identical inputs are idempotent.
- Re-reading a transcript never duplicates a row (natural keys everywhere).
- Nothing content-like reaches the store, exports, findings or errors.
- Each tool call contributes to at most one finding.

The full design, including the decisions behind the data model, is in
`DESIGN.md`; source capabilities in `AGENT_SOURCES.md`.
