# Privacy and local storage

RunPeek runs on your machine, makes no network calls of its own, has no
account, and uploads nothing. Everything it keeps is in one SQLite file.

## Where

`./.runpeek/runpeek.db` in the directory you run commands from (override with
`--db` or `RUNPEEK_DB`). The directory is created `0700`, the file `0600`.
Delete the file to delete everything.

## What is stored

| Source | Stored | Never stored |
|---|---|---|
| `runpeek run` (OpenAI SDK harness) | run id, a redacted command description, timestamps, latency, model names, token counts, provider request ids, HTTP status, error class, customer/job labels you set, cost estimates and their provenance | prompts, messages, completions, request or response bodies, API keys, headers |
| `runpeek watch` (Claude Code transcripts, Codex rollouts) | session/turn/tool-call ids, timestamps, tool names, an allowlisted target (path relative to the project, program name, or URL host), error flag, token counts, model names, provider response ids, git branch name, a keyed fingerprint of normalised tool arguments | prompts, assistant text, reasoning, tool inputs and outputs, commands, patches, file contents, URLs beyond the host, commit hashes, instructions |
| `runpeek work` | the names, kinds, references (issue, branch, PR, deployment) and notes you type, assignment history | — |

Findings, exports and error messages are built only from stored fields.

## Fingerprints

To detect "the same action again" without keeping the action, RunPeek stores
an HMAC-SHA256 of the normalised arguments keyed with a random 32-byte key
generated per store. The key lives in the store's `meta` table, which
`runpeek export` never includes. Treat fingerprints as sensitive derived data:
low-entropy inputs could in principle be guessed by someone who also holds
the key.

## Identifier-bearing fields

Customer and job labels, relative paths, program names and hostnames are
metadata you or your tools chose; they can still be sensitive. Pass opaque
customer ids if that matters to you.

## The recorded command line

`runpeek run` stores a *description* of the launched command, not the command:
inline `-c` programs, whitespace-bearing arguments, brace/JSON payloads, URL
userinfo and query strings, and secret-looking option values or positionals
are replaced with `<redacted>`. This is a heuristic; it is documented and
tested, not guaranteed.

## Export

`runpeek export --format jsonl` writes the stored tables as JSON lines with
exact monetary values (`amount_nanos` and a lossless `amount_usd` string).
Review an export before sharing it: it contains the metadata above.
