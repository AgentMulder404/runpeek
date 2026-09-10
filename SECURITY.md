# Security

## What RunPeek does with data

- Runs entirely on your machine. It makes no network calls of its own and has
  no runtime dependencies.
- Stores only allowlisted metadata in a local SQLite file (`./.runpeek/runpeek.db`,
  created `0600`): tool names, relative paths / program names / hosts,
  timestamps, token counts, model names, ids, and keyed fingerprints. It never
  stores prompts, completions, tool inputs or outputs, commands, file contents
  or credentials. Details: `docs/PRIVACY.md`.
- The `runpeek run` bootstrap patches one method of the OpenAI Python SDK in
  the launched process. It never retries or re-issues a provider call.

## Reporting a vulnerability

A private reporting channel has **not** been verified yet. The intended
public home is `github.com/AgentMulder404/runpeek`; GitHub private
vulnerability reporting will be enabled there as part of the release
checklist (`docs/RELEASE_CHECKLIST.md`, step 2), and this section will then
link to it.

Until that link appears here, please do not open public issues for
vulnerabilities that could expose other users' data.

## Supported versions

Pre-release. Fixes are made on the main branch only.
