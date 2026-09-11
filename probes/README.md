# Phase 0 probes

Throwaway scripts used to verify platform capabilities with real sessions
(results in `docs/EVIDENCE_MATRIX.md`). They are not part of the package.

- `otlp_receiver.py PORT OUT.jsonl` — minimal OTLP/HTTP receiver that records
  every request (JSON bodies decoded, binary bodies as sizes). Used to capture
  Claude Code and Codex telemetry:

  ```sh
  python3 probes/otlp_receiver.py 4319 /tmp/claude_otel.jsonl &
  CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_LOGS_EXPORTER=otlp OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319 OTEL_LOGS_EXPORT_INTERVAL=500 \
    claude -p --model claude-haiku-4-5-20251001 "Reply with the single word: ok" < /dev/null

  npx -y @openai/codex@latest exec --skip-git-repo-check -s read-only --json \
    -c 'otel.exporter={ otlp-http = { endpoint = "http://127.0.0.1:4319/v1/logs", protocol = "json" } }' \
    "Reply with the single word: ok" < /dev/null
  ```

- `probe_mcp.py` — dependency-free stdio MCP server with one tool that records
  the environment and `_meta` the host gives it. Register temporarily
  (`claude mcp add -s user runpeek-probe -- python3 probes/probe_mcp.py`), run a
  session, inspect `~/runpeek-probe-env.json`, then remove the registration.

Sanitise captured payloads before committing them (see how
`tests/fixtures/otel/` was produced in the evidence matrix): replace emails,
account ids, organisation ids and host names.
