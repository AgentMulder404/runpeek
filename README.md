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
nemulai · run run_…  ·  python examples/basic.py  ·  ended cleanly
perspective default  ·  rates list  ·  cards openai-list@2025-08-01  ·  calc 1

OPERATIONS        8 attempts (8 operations)        adapters: openai.chat.completions.create
  completed 6      provider_error 2      in_progress 0

KNOWN ESTIMATED COST   $0.0138…   list-price estimate, not actual spend   [deduplicated (exact identity only)]
  priced              5 charges   …
  unpriced            1 charges   model not in rate card: "acme-preview-1" (1)
  no_usage            2 charges   2 errors
  billing         expected 6 · unknown 2 · confirmed 0 · not_billed 0

COVERAGE          usage: exact 6 / missing 0 of 6 completed   ·   pricing: 5 of 6 with usage priced   ·   capture: not measurable
ATTRIBUTION       by cost: attributed … · job_only … · unattributed … 

BY CUSTOMER   …      BY MODEL   …      TELEMETRY   records … · dropped 0 · unflushed at exit 0 · …
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
- **Rates** come from immutable, dated rate cards. The shipped
  `openai-list@2025-08-01` is a transcription — verify it against
  openai.com/pricing before relying on it, or pass your own with `--rate-card
  file.json`. The card effective at the attempt's start time is used; a
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
