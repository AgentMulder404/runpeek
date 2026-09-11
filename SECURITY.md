# Security

## What RunPeek does with data

- Local collection and accounting make no model calls. Network traffic is opt-in via
  `connect` and `sync`; the optional receiver supports private cross-device aggregation.
  The sync extra uses the OS credential store. See `docs/UNIFIED.md` for trust boundaries,
  deployment requirements, deletion scope and remaining production work.
- Stores only allowlisted metadata in a local SQLite file (`./.runpeek/runpeek.db`,
  created `0600`): tool names, relative paths / program names / hosts,
  timestamps, token counts, model names, ids, and keyed fingerprints. It never
  stores prompts, completions, tool inputs or outputs, commands, file contents
  or credentials through the agent collectors. The legacy SDK command-description
  redaction is heuristic, and user-provided metadata can still be sensitive.
  Details: `docs/PRIVACY.md`.
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
