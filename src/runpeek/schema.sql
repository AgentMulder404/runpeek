-- RunPeek store, schema version 1.
-- Every statement is idempotent so the file can be applied on every start.
-- Money is integer nanodollars (see money.py). Timestamps are ISO-8601 UTC text.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE IF NOT EXISTS runs (
  run_id            TEXT PRIMARY KEY,
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  clean_exit        INTEGER,               -- 1 when close() completed; NULL if never closed
  command           TEXT,
  pid               INTEGER,
  heartbeat_at      TEXT,
  records_written   INTEGER NOT NULL DEFAULT 0,
  dropped_confirmed INTEGER,
  unflushed_known   INTEGER,
  persist_failures  INTEGER,
  harness_version   TEXT,
  app_exit_status   INTEGER                -- written by `runpeek run` after the child exits; negative = -signal; NULL = unknown
);

CREATE TABLE IF NOT EXISTS spans (
  span_id        TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  parent_span_id TEXT,
  kind           TEXT NOT NULL,             -- job (M1)
  name           TEXT,
  customer_id    TEXT,
  job_name       TEXT,
  job_id         TEXT,
  parent_job_id  TEXT,
  attributes     TEXT,                      -- JSON object of string pairs
  started_at     TEXT,
  ended_at       TEXT,
  status         TEXT
);

-- One application-level call of a wrapped SDK method.
CREATE TABLE IF NOT EXISTS operations (
  operation_id      TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  span_id           TEXT,                   -- innermost job span, or NULL (implicit model_call)
  surface           TEXT NOT NULL,          -- e.g. openai.chat.completions.create
  supported         INTEGER NOT NULL DEFAULT 1,
  started_at        TEXT,
  customer_id       TEXT,
  job_name          TEXT,
  job_id            TEXT,
  attribution_state TEXT NOT NULL           -- attributed | job_only | unattributed
);
CREATE INDEX IF NOT EXISTS ix_operations_run ON operations (run_id);

-- One observable provider request. The accounting unit.
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id        TEXT PRIMARY KEY,
  operation_id      TEXT NOT NULL,
  run_id            TEXT NOT NULL,
  visibility        TEXT NOT NULL,          -- last_only | single | aggregate
  provider          TEXT NOT NULL,
  model_requested   TEXT,
  model_served      TEXT,
  started_at        TEXT,
  ended_at          TEXT,
  latency_ms        REAL,
  observed_status   TEXT NOT NULL DEFAULT 'in_progress',
  error_class       TEXT,
  http_status       INTEGER,
  start_missing     INTEGER NOT NULL DEFAULT 0,
  diagnostic_status TEXT                    -- written only by doctor (planned)
);
CREATE INDEX IF NOT EXISTS ix_attempts_operation ON attempts (operation_id);
CREATE INDEX IF NOT EXISTS ix_attempts_run ON attempts (run_id);

-- Identity evidence. An identifier value of a given kind resolves to at most
-- one subject; two sources presenting the same one are exactly correlated.
CREATE TABLE IF NOT EXISTS identifiers (
  id_kind      TEXT NOT NULL,
  namespace    TEXT NOT NULL,
  value        TEXT NOT NULL,
  subject_kind TEXT NOT NULL,               -- attempt | operation
  subject_id   TEXT NOT NULL,
  source       TEXT NOT NULL,
  PRIMARY KEY (id_kind, namespace, value)
);
CREATE INDEX IF NOT EXISTS ix_identifiers_subject ON identifiers (subject_kind, subject_id);

-- What one source said about one attempt. Normalised, allowlisted fields
-- only; never merged, edited or deleted by correlation or selection.
CREATE TABLE IF NOT EXISTS source_observations (
  observation_id      TEXT PRIMARY KEY,
  attempt_id          TEXT NOT NULL,
  source              TEXT NOT NULL,        -- harness.openai | proxy | framework | otel | user
  collected_at        TEXT NOT NULL,
  model_served        TEXT,
  input_tokens        INTEGER,
  cached_input_tokens INTEGER,
  output_tokens       INTEGER,
  reasoning_tokens    INTEGER,
  usage_source        TEXT NOT NULL,        -- exact | estimated | missing
  http_status         INTEGER,
  selected            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_observations_attempt ON source_observations (attempt_id);

CREATE TABLE IF NOT EXISTS correlations (
  correlation_id TEXT PRIMARY KEY,
  observation_a  TEXT NOT NULL,
  observation_b  TEXT NOT NULL,
  class          TEXT NOT NULL,             -- exact | inferred | conflicting | rejected | confirmed
  evidence       TEXT NOT NULL,
  decided_by     TEXT NOT NULL,             -- system | operator
  created_at     TEXT NOT NULL
);

-- A potential or confirmed expenditure. One per attempt.
CREATE TABLE IF NOT EXISTS charges (
  charge_id      TEXT PRIMARY KEY,
  attempt_id     TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL,             -- provider (M1)
  billing_status TEXT NOT NULL,             -- expected | unknown | confirmed | not_billed
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS perspectives (
  perspective_id TEXT PRIMARY KEY,
  rates          TEXT NOT NULL,             -- list | contract | reconciled
  resolution     TEXT NOT NULL,             -- effective_at_execution
  fallback       TEXT NOT NULL,             -- nearest_earlier | nearest | none
  pin            TEXT,                      -- rate_card_id, optional
  created_at     TEXT NOT NULL
);

-- One estimate per (charge, perspective, estimate_key). Immutable rows;
-- exactly one row per (charge, perspective) has current = 1.
CREATE TABLE IF NOT EXISTS cost_estimates (
  estimate_key            TEXT PRIMARY KEY,
  charge_id               TEXT NOT NULL,
  perspective_id          TEXT NOT NULL,
  revision                INTEGER NOT NULL,
  current                 INTEGER NOT NULL DEFAULT 1,
  superseded_by           TEXT,
  estimation_status       TEXT NOT NULL,    -- priced | unpriced | no_usage | partial
  amount_nanos            INTEGER,          -- NULL unless priced
  currency                TEXT NOT NULL DEFAULT 'USD',
  rate_card_id            TEXT,
  rate_resolution         TEXT,             -- effective_at_execution | fallback_nearest_earlier | fallback_nearest_later | pinned | none
  usage_provenance        TEXT NOT NULL,    -- exact | estimated | missing
  rate_provenance         TEXT NOT NULL,    -- list | contract | statement | none
  completeness            TEXT NOT NULL,    -- complete | partial
  selected_observation_id TEXT,
  calc_version            INTEGER NOT NULL,
  detail                  TEXT,             -- JSON: which fields were missing, model key used
  created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_estimates_charge ON cost_estimates (charge_id, perspective_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_estimates_current
  ON cost_estimates (charge_id, perspective_id) WHERE current = 1;

CREATE TABLE IF NOT EXISTS health_events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id  TEXT NOT NULL,
  at      TEXT NOT NULL,
  kind    TEXT NOT NULL,                    -- adapter_installed | adapter_failed | hook_failure_before | hook_failure_after | unsupported_surface | ...
  detail  TEXT
);
CREATE INDEX IF NOT EXISTS ix_health_run ON health_events (run_id);

-- ---------------------------------------------------------------------------
-- Coding-agent observer (runpeek watch). Separate from the SDK harness tables:
-- agent usage is never mixed into the M1 attempt/charge totals.
-- Only allowlisted metadata is stored; never prompts, tool payloads or file
-- contents. Fingerprints are keyed HMACs and are treated as sensitive.

CREATE TABLE IF NOT EXISTS agent_sessions (
  session_id          TEXT PRIMARY KEY,
  source              TEXT NOT NULL,             -- claude-code
  source_version      TEXT,                      -- writer version seen in the transcript
  format_status       TEXT NOT NULL DEFAULT 'supported',  -- supported | unknown_version | unparseable
  project_path        TEXT,
  transcript_path     TEXT NOT NULL,
  parent_session_id   TEXT,
  is_subagent         INTEGER NOT NULL DEFAULT 0,
  first_event_at      TEXT,
  last_event_at       TEXT,
  first_seen_at       TEXT NOT NULL,
  last_ingested_at    TEXT,
  entries_ingested    INTEGER NOT NULL DEFAULT 0,
  entries_unparseable INTEGER NOT NULL DEFAULT 0,
  customer_id         TEXT,                      -- explicit mapping only (runpeek session <id> --set-customer)
  job_name            TEXT,
  provider            TEXT,                      -- anthropic | openai (model provider behind the agent)
  git_branch          TEXT,                      -- first branch the source reported; a hint, never an assignment
  repository_url      TEXT,
  usage_duplicates    INTEGER NOT NULL DEFAULT 0, -- usage records already counted under another session
  duplicate_of_session_id TEXT,                  -- the session that owns those records (resumed/forked copy)
  usage_consistency   TEXT,                      -- JSON counters from the adapter (stale repeats, resets, ...)
  telemetry_last_at   TEXT                       -- last documented-telemetry event for this session (collection health)
);

CREATE TABLE IF NOT EXISTS agent_turns (
  turn_id            TEXT PRIMARY KEY,           -- the source's prompt id when present
  session_id         TEXT NOT NULL,
  started_at         TEXT,
  ended_at           TEXT,
  duration_ms        INTEGER,                    -- source-reported when available
  assistant_messages INTEGER NOT NULL DEFAULT 0,
  tool_calls         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_agent_turns_session ON agent_turns (session_id);

CREATE TABLE IF NOT EXISTS agent_actions (
  action_id     TEXT PRIMARY KEY,                -- the source's tool_use id
  session_id    TEXT NOT NULL,
  turn_id       TEXT,
  sequence      INTEGER NOT NULL,                -- ingest order within the session
  tool_name     TEXT NOT NULL,
  action_kind   TEXT NOT NULL,                   -- read | edit | write | bash | search | web | mcp | other
  target        TEXT,                            -- allowlisted: relative path, program name, or host
  fingerprint   TEXT NOT NULL,                   -- keyed HMAC of normalised arguments (sensitive)
  requested_at  TEXT,
  completed_at  TEXT,
  duration_ms   REAL,
  is_error      INTEGER                          -- 1 | 0 | NULL (no result observed)
);
CREATE INDEX IF NOT EXISTS ix_agent_actions_session ON agent_actions (session_id, sequence);

CREATE TABLE IF NOT EXISTS agent_usage (
  usage_id              TEXT PRIMARY KEY,        -- the source's message id (dedupes repeated entries)
  session_id            TEXT NOT NULL,
  turn_id               TEXT,
  request_id            TEXT,
  model                 TEXT,
  at                    TEXT,
  input_tokens          INTEGER,
  cache_write_5m_tokens INTEGER,
  cache_write_1h_tokens INTEGER,
  cache_read_tokens     INTEGER,
  output_tokens         INTEGER,
  web_search_requests   INTEGER,
  web_fetch_requests    INTEGER,
  usage_kind            TEXT NOT NULL,           -- per_request | cumulative_snapshot
  provenance            TEXT NOT NULL,           -- provider_reported | estimated
  source_cost_nanos     INTEGER,                 -- source-native reported cost; NULL when the source reports none
  api_equiv_nanos       INTEGER,                 -- rate-card calculation; NOT a subscription charge
  api_equiv_status      TEXT NOT NULL,           -- priced | unpriced | no_usage
  rate_card_id          TEXT,
  rate_resolution       TEXT,
  calc_version          INTEGER,
  provider              TEXT,                    -- anthropic | openai
  reasoning_tokens      INTEGER,                 -- part of output_tokens (informational)
  ordinal               INTEGER,                 -- position of the source record, for tracing
  telemetry_at          TEXT,                    -- when the documented telemetry observation arrived
  quarantine_reason     TEXT                     -- set when api_equiv_status = 'quarantined'
);
CREATE INDEX IF NOT EXISTS ix_agent_usage_session ON agent_usage (session_id);

-- A usage record seen again under a different session (a resumed or forked
-- transcript copies history). It is counted once, under the owner; this row
-- keeps the evidence so a report can say what was shared.
CREATE TABLE IF NOT EXISTS agent_usage_duplicates (
  usage_id         TEXT NOT NULL,
  session_id       TEXT NOT NULL,                -- the session that saw the copy
  owner_session_id TEXT NOT NULL,                -- the session it is counted under
  seen_at          TEXT NOT NULL,
  PRIMARY KEY (usage_id, session_id)
);

CREATE TABLE IF NOT EXISTS agent_findings (
  finding_id  TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,              -- kind + session + evidence span; changes when evidence grows
  group_key   TEXT,                              -- kind + session + first evidence id; one row per group (consolidated)
  session_id  TEXT NOT NULL,
  turn_id     TEXT,
  kind        TEXT NOT NULL,                     -- repeated_failing_action | repeated_read | retry_loop
  severity    TEXT NOT NULL DEFAULT 'potential_inefficiency',
  summary     TEXT NOT NULL,
  evidence    TEXT NOT NULL,                     -- JSON: action ids + timestamps
  counts      TEXT NOT NULL,                     -- JSON: observed counts / durations / usage
  limitations TEXT NOT NULL,
  suggestion  TEXT NOT NULL,
  first_at    TEXT,
  last_at     TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_findings_session ON agent_findings (session_id);

CREATE TABLE IF NOT EXISTS watch_checkpoints (
  transcript_path TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  inode           INTEGER,
  size            INTEGER,
  offset          INTEGER NOT NULL,              -- byte offset of the first unconsumed line
  line_no         INTEGER NOT NULL,
  head_sha        TEXT,                          -- sha256 of the first bytes; detects rewrites that reuse an inode
  updated_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Work items: the unit of accounting across sessions, agents, models, retries
-- and subagents. A session belongs to at most one work item (primary key on
-- session_id), so no usage can be counted under two items. Assignment is
-- explicit; repository/branch/issue only *suggest*.

CREATE TABLE IF NOT EXISTS work_items (
  work_item_id   TEXT PRIMARY KEY,               -- wi-<6 hex>
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL,                  -- task | feature | bugfix | deployment
  repository     TEXT,                           -- project path or repository url, as the user gave it
  status         TEXT NOT NULL DEFAULT 'open',   -- open | closed
  outcome        TEXT,                           -- completed | incomplete | failed | abandoned (NULL while open)
  issue_ref      TEXT,
  branch         TEXT,
  pr_ref         TEXT,
  deployment_ref TEXT,
  note           TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  closed_at      TEXT
);

CREATE TABLE IF NOT EXISTS work_item_sessions (
  session_id   TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  assigned_at  TEXT NOT NULL,
  assigned_by  TEXT NOT NULL DEFAULT 'user'      -- user (explicit). Suggestions are never written here.
);
CREATE INDEX IF NOT EXISTS ix_work_item_sessions_item ON work_item_sessions (work_item_id);

CREATE TABLE IF NOT EXISTS work_item_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id TEXT NOT NULL,
  at           TEXT NOT NULL,
  kind         TEXT NOT NULL,                    -- created | assigned | reassigned | unassigned | status | edited
  detail       TEXT                              -- JSON
);
CREATE INDEX IF NOT EXISTS ix_work_item_events_item ON work_item_events (work_item_id);

-- Participants without measured usage: browser conversations and other surfaces
-- that take part in a task but expose no token accounting. Identity is a keyed
-- hash of the platform's own conversation id; the id itself is never stored.
CREATE TABLE IF NOT EXISTS work_item_participants (
  participant_id  TEXT PRIMARY KEY,              -- pt-<hex>
  work_item_id    TEXT NOT NULL,
  platform        TEXT NOT NULL,                 -- chatgpt | claude-web | other
  conversation_fp TEXT NOT NULL,                 -- keyed HMAC of the conversation id
  label           TEXT,                          -- user-supplied, optional
  metering        TEXT NOT NULL DEFAULT 'unmetered',
  attached_at     TEXT NOT NULL,
  UNIQUE (platform, conversation_fp)
);
CREATE INDEX IF NOT EXISTS ix_participants_item ON work_item_participants (work_item_id);
