# nemulai

Local-first observability and efficiency harness for AI workloads.

Run an existing Python application under `nemulai run` and get, on your own
machine, an accounting of every AI call the harness observed: who it was for,
what it used, what it is estimated to cost, and — just as loudly — what could
not be measured. No account, no service, no network access for telemetry, and
no prompt, completion, API-key or request-body capture.

**This is milestone M1.** It observes exactly one surface: the OpenAI Python
SDK's synchronous, non-streaming `chat.completions.create`. See the support
table before assuming anything else is covered.

## Five minutes, offline

```bash
git clone <this repo> nemulai-harness && cd nemulai-harness
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # dev extras bring the openai SDK used by the example
nemulai run python examples/basic.py
```

The example is a small "support assistant" that serves three customers through
the real `openai` client with a local mock transport — no key, no network. When
it exits, `nemulai run` prints the run's summary:

```
NEMULAI / RUN COMPLETE

python examples/basic.py
Application exited successfully · telemetry saved

8 model calls observed
  6 completed · 2 failed

$0.016052  known estimated API cost
           5 of 8 calls priced — total is incomplete

CUSTOMER           ESTIMATED COST    UNPRICED / UNKNOWN
acme                     $0.01486                     0
globex                   $0.00084                     1
No customer tag         $0.000352                     0
initech                         —                     2

Missing from this estimate:
• 1 call used a model with no known price (acme-preview-1)
• 2 failed calls returned no usage

Estimate at list prices — not verified provider billing.

Stored locally · nothing uploaded
Details: nemulai events · Full accounting: nemulai summary --verbose
```

Then:

```bash
nemulai summary                     # latest run, default perspective
nemulai events --last 20            # recent attempts
nemulai export --format jsonl --out run.jsonl
nemulai reprice --pin openai-list@2025-08-01   # price under a separate pinned perspective
```

The store is a SQLite file at `./.nemulai/nemulai.db` (override with `--db` or
`NEMULAI_DB`), created with mode `0600`. Open it with anything.

## Your own application

```bash
export OPENAI_API_KEY=…             # your key; the harness never reads or stores it
nemulai run python examples/real_openai.py   # real-provider smoke test, ~$0.0001
nemulai run python app.py
```

Attribute calls from inside the code:

```python
import nemulai

with nemulai.job(customer="acme", job="support_ticket"):
    client.chat.completions.create(...)     # observed and attributed
```

Nested `job()` inherits `customer`, gets a fresh `job_id`, and records the
parent. Calls outside any `job()` are recorded as **unattributed** — a real
row with a real cost, not a missing one. Context follows `await` and
`asyncio.create_task`; it does not follow threads — use `nemulai.wrap(fn)`
for executors and `nemulai.inject()` / `nemulai.extract()` across processes.

If you cannot use `nemulai run`, call `nemulai.install()` before the provider
client is imported.

## Watching a coding agent (Claude Code)

Run your coding agent normally. In another terminal:

```bash
nemulai watch                      # this project; transcripts modified in the last 7 days, then live
nemulai watch --history none       # only what happens from now on
nemulai watch --all-projects       # every project Claude Code has sessions for
```

It starts with a short status block, loads recent history (summarised, never
replayed as if it were happening now), and then prints only meaningful events:

```
NEMULAI / LIVE WATCH

Watching Claude Code in AluminatiAi
Use Claude Code normally. This terminal shows activity
and potential inefficiencies as they appear.

Local collection · no uploads
Prompts and file contents are not stored.
File paths and usage metadata are stored.

Loading recent history…
Ready · 3 sessions loaded (4 transcript files found, 1 empty)
Historical: 2 potential inefficiencies in 2 sessions (not replayed here) · nemulai findings --project /Users/dev/AluminatiAi

New activity appears below.
Ctrl-C stops watching. Your Claude session keeps running.

05:03  REPEATED READ
       package.json was read 4 times in 2 minutes.
       No edit to that file was observed between reads.
       Repeated billing cannot be determined.

       Evidence: nemulai session 719c4032

05:04  TURN FINISHED
       51 seconds · 8 tool calls (4 failed) · 8 model calls
       API-equivalent estimate: $0.115
       2 potential inefficiencies to review · nemulai session 719c4032
```

Polling internals and per-file counts are behind `--verbose`. Ctrl-C stops it;
checkpoints mean a restart never duplicates anything. Then:

```bash
nemulai sessions                   # this project: started, tool calls, errors, model calls, items to review
nemulai sessions --detailed        # adds tokens and the API-equivalent estimate per session
nemulai session <id-prefix>        # activity, turns, potential inefficiencies with evidence, usage and cost
nemulai findings                   # potential inefficiencies across sessions
nemulai session <id> --set-customer acme --set-job refactor   # explicit mapping only; never inferred
```

Supported source: **Claude Code 2.1.x** transcripts (CLI 2.1.258 verified; the
file format is undocumented, so the adapter is versioned and experimental — other
writer versions are parsed best-effort and marked `unknown_version`). Codex is
not yet supported; the capability check and adapter path are in
[`docs/AGENT_SOURCES.md`](docs/AGENT_SOURCES.md).

What the numbers are:

- **Tokens** are provider-reported per API request, deduplicated by message id
  (Claude Code writes several transcript entries per streamed response).
- **≈$ API-equivalent** is a rate-card calculation at Anthropic list prices
  (`anthropic-list@2026-09-09`). For subscription users it is **not** a charge,
  a quota, or a saving. Source-reported cost is not available in transcripts
  (only in Claude Code's OpenTelemetry export, which this does not consume).
- Sessions belong to a **project**, never to a customer, unless you map them.
- Agent usage is kept apart from the SDK harness tables and is never added into
  `nemulai summary`'s totals.

Three checks, all deterministic and local, each reported as a *potential
inefficiency* with what was observed, evidence, a next step and its limitation.
Each tool call is counted in at most one item; items grow in place rather than
duplicating, and the live feed rate-limits updates to one per minute per item.

| Item | Evidence | What it does not claim |
|---|---|---|
| REPEATED FAILURE | the same command or tool call failed ≥ 3 times with no edit or write observed between | that a command side effect didn't change something |
| REPEATED READ | the same file read ≥ 3 times with no edit to it observed between | that its contents were re-billed, or any saving |
| REPEATED TOOL ERRORS / TIGHT LOOP | ≥ 4 consecutive errors from one tool across different inputs inside 10 min, or the same succeeding action ≥ 5 times inside 3 min | that a gap, permission wait or user pause is a stall |

Privacy: only allowlisted metadata is stored — tool name, a relative path /
program name / host, timestamps, error flag, token counts, and a **keyed
fingerprint** of the normalised arguments (HMAC with a per-store random key that
never leaves the store and is excluded from export). No prompts, tool inputs,
outputs, commands or file contents. Fingerprints are treated as sensitive derived
data. The store is created `0600`.

Measured on this machine (Apple Silicon, Python 3.12, 2026-09-09) — measurements,
not targets: idle watcher 0.0 % CPU averaged over 20 s, 18.5 MB RSS; ingest of a
synthetic 8.4 MB / 10,101-line transcript in 0.27 s wall including interpreter
start and diagnostics (~37k lines/s); an incremental tick with 100 new lines
0.14 s wall including interpreter start; live ingest of this project's 80 real
transcripts (52,311 entries, 9,652 requests) in ~1.0 s.

## What is supported (tested), and what is not

Tested against `openai==2.44.0` (pinned in the dev extras; the adapter's
claims were verified against that source). Python ≥ 3.10.

| Surface | Status |
|---|---|
| `client.chat.completions.create(...)` sync, non-streaming | **supported** (gate G1) |
| `...create(..., stream=True)` | recorded as an attempt with `unsupported_stream`, no usage, no cost; the stream is returned untouched |
| `AsyncOpenAI` / `AsyncCompletions` | **not patched** — invisible to M1 |
| `client.responses.create` | not patched |
| `with_raw_response.create` | covered when the harness was installed before the property was first accessed (it caches the original bound method otherwise — tested both ways) |
| Other providers, raw HTTP, other languages | not observed |

| Launch mode (`nemulai run <cmd>`) | Status |
|---|---|
| `python script.py`, `python -c` | **tested** |
| `python -m pkg`, `uvicorn`, `gunicorn`, `celery` | expected to work (they start a CPython interpreter that processes `site` and inherits the environment) — **not yet tested; not claimed** |
| `python -S` / `-I`, embedded interpreters, launchers that scrub the environment, non-Python processes | **not covered** (tested: `-S` produces no store) |
| a process that imports `openai` before `site` runs | not covered |

An existing `sitecustomize` elsewhere on `PYTHONPATH` is chained, not
replaced (tested).

What the SDK layer cannot see, by construction: the SDK retries 408/409/429/5xx,
timeouts and connection errors up to `max_retries` (default 2) *below* the
patched method, so one call is one operation and the **last** attempt only.
Whether an earlier failed attempt was billed is unknowable from the client and
is reported as `billing: unknown`.

## What the numbers mean

- **Known estimated cost** is a list-price estimate under one *perspective*
  (rate card source + resolution rule). It is not actual spend. Everything that
  could not be priced is counted beside it: `unpriced` (model not in the rate
  card), `no_usage` (errors, in-progress, unsupported), `partial` (lower bound).
- **Unknown is never zero.** A missing usage block, an unknown model, an error
  or a timeout produce a charge with an unknown or unpriced status, not `$0`.
- **Rates** come from immutable, dated rate cards. `openai-list@2025-08-01`
  is unchanged from its first release; `openai-list@2026-09-09` and
  `anthropic-list@2026-09-09` carry prices as published on their verification
  date (their `effective_from` is that date, not evidence of when the prices
  began). Among cards covering an attempt's date the newest wins, so historical
  estimates keep their card and value (tested). Pass your own with
  `--rate-card file.json`. The card effective at the attempt's start time is used; a
  fallback is recorded when none covers it. There is no "latest" default.
- **Cached and reasoning tokens** are subsets of prompt and completion tokens
  (per the SDK's usage types) and are never charged twice: cost =
  (prompt − cached) × input + cached × cached-input + completion × output.
- **Repricing** creates estimates under a separate pinned perspective; the
  default perspective's history is preserved. Re-running with identical inputs
  creates nothing.
- **Money** is integer nanodollars in the store and lossless decimal strings in
  the export (`amount_usd`).
- **Coverage** is reported as instrumentation health, usage coverage, pricing
  coverage and attribution coverage, each with its denominator. *Capture*
  coverage — how much AI activity the harness did not see — is `not
  measurable` without an independent source, and a run with no observations
  says so rather than implying no spend.
- **Telemetry loss** is reported per run: confirmed drops (bounded queue,
  default 10,000), records unflushed at the shutdown deadline (default 5 s),
  and persist failures. After a crash the run has no end record and the
  summary says so; loss after the last heartbeat is unknown.

## Guarantees

- The wrapped SDK method is invoked exactly once per call; its return value or
  exception is passed through unchanged. A hook failure before or after the
  call is counted in `health_events` and never re-issues the call (tested).
- Hook installation is idempotent (tested).
- No prompts, completions, request bodies or credentials are recorded. Source
  observations hold allowlisted usage fields, ids, timing and status only.
- The application's exit status is preserved; death by signal maps to 128+N.
  The summary reports **application exit** and **telemetry shutdown** as two
  separate facts — an app can fail while telemetry ends cleanly, and vice versa.
- The recorded command line is a redacted description: inline `-c` programs,
  whitespace-bearing arguments and values of secret-looking options are stored
  as `<redacted>`, never verbatim.
- After a crash the run has no final counters; the summary counts what reached
  the store and says drops/unflushed/persist-failures are unknown.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
mypy
```

Design: `docs/DESIGN.md` (revision 3). Benchmarks in `bench/` are targets
until measured; none have been measured yet.

## License

Not yet decided (see `docs/DESIGN.md` §13). Not published to PyPI.
