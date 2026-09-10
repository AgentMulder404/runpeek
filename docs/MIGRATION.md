# Migrating from the NemulAI harness to RunPeek

RunPeek is the same code base renamed. Your collected data is compatible; only
names changed.

| Before | After | Compatibility |
|---|---|---|
| `pip install -e .` gave distribution `nemulai-harness`, package `nemulai` | distribution `runpeek`, package `runpeek` | **No shim.** `import nemulai` is not provided because the `nemulai` name on PyPI belongs to a different project (a GPU telemetry agent). Change imports to `runpeek`. |
| `nemulai run …`, `nemulai watch …` | `runpeek run …`, `runpeek watch …` | Same subcommands and options. |
| `NEMULAI_DB`, `NEMULAI_ENABLED`, `NEMULAI_CLAUDE_HOME`, … | `RUNPEEK_DB`, `RUNPEEK_ENABLED`, `RUNPEEK_CLAUDE_HOME`, … | Legacy names still honoured; `RUNPEEK_*` wins when both are set. Deprecated; will be removed in a later release. |
| `./.nemulai/nemulai.db` | `./.runpeek/runpeek.db` | See below. |
| `nemulai-customer` / `nemulai-job` carrier keys from `inject()` | `runpeek-customer` / `runpeek-job` | `extract()` accepts both. |
| Thread name `nemulai-writer` | `runpeek-writer` | Cosmetic. |

## The store

The SQLite schema is unchanged; stored values (`source = 'harness.openai'`,
rate-card ids, `claude-code`) are unchanged. Nothing needs to be converted.

When no `./.runpeek/runpeek.db` exists but `./.nemulai/nemulai.db` does, every
command uses the legacy file **in place** and prints one line to stderr:

```
runpeek: using legacy store .nemulai/nemulai.db — move it to .runpeek/runpeek.db to silence this (docs/MIGRATION.md)
```

RunPeek never copies, converts or deletes a legacy store, and never creates an
empty new store next to an existing legacy one. To move:

```bash
mkdir -p .runpeek && mv .nemulai/nemulai.db .runpeek/runpeek.db
# if a -wal / -shm file exists next to it, move those too
```

Once `./.runpeek/runpeek.db` exists it is always preferred; a leftover
`./.nemulai/` directory is then ignored.

## Verifying

```bash
runpeek sessions --project /path/to/project     # your Claude Code sessions are still listed
runpeek summary                                 # your latest harness run is still there
```
