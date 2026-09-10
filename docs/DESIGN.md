# NemulAI OSS — Design, revision 3 (implementation-ready)

Local-first observability and efficiency harness for AI workloads.
Status: final design, 2026-09-09. No code exists yet.

Status vocabulary: **v0** (first release) · **gated** (implemented only when its named test
gate passes) · **planned** (a named later release) · **design target** (a number that becomes a
claim only when measured) · **verified** (checked against the installed `openai` 2.44.0
source on 2026-09-09; file and line cited) · **unverified** (stated as belief).

---

## 1. Recommendation and boundary

Build a local-first harness whose accounting is correct before it is convenient. The first
milestone is one provider path end to end — the OpenAI Python SDK, `chat.completions.create`,
synchronous, non-streaming, launched as `runpeek run python script.py` — with the full
accounting model underneath it. Streaming, async, the Responses API and server launch modes
are added through tested gates, not claimed from intent.

**Open source.** Everything that measures, explains, diagnoses, compares, or recommends
improvements to AI workload cost, performance, reliability and outcome efficiency — and
everything needed to operate the harness safely: local-only binding by default, access
protection for any exposed dashboard, and protection of locally stored data. It runs with no
NemulAI account, license server or cloud service.

**Commercial.** Organisational governance: policy, contractual allowances, approval
workflows, enforcement that acts on workload behaviour, and multi-team organisational
identity. Governance consumes OSS records through the export format and never gates them.
Enforcement, when it exists, is a separate integration the operator installs and can remove.

Rule for reviewers: securing the tool is OSS; deciding who in an organisation may do what is
commercial. Dashboard authentication is the former.

---

## 2. Data model

### 2.1 Records

| Record | One row per | Purpose |
|---|---|---|
| **Run** | harness-launched process tree, or `install()` call | groups records; carries start, heartbeat, clean end, final counters |
| **Span** | execution unit: job, workflow, agent step, tool, retrieval, retry group, model call | structure and **attribution**; never cost or tokens |
| **Operation** | one application-level call of a wrapped SDK method | what the app did once; parent of attempts |
| **Attempt** | one observable provider request | the accounting unit for usage and charges; has lifecycle status |
| **Source observation** | one source's report of one attempt (or, if the source cannot see attempts, of one operation) | evidence, kept verbatim with provenance; never merged or deleted |
| **Identifier** | one (kind, namespace, value) attached to an attempt or operation | identity evidence; kinds are not interchangeable |
| **Correlation** | one link between two source observations, or an observation and an attempt | evidence class, tolerance, decision |
| **Charge** | one potential or confirmed expenditure | billing status × estimation status; parent of estimates |
| **Cost estimate** | one (charge, perspective, estimate key) | an amount with full provenance; immutable; revisions chain |
| **Rate card** | one immutable pricing version | prices per model with an effective window |
| **Billing fact** | one statement/usage-export line (planned) | reconciliation evidence |
| **Resource sample** | (pool, timestamp) (planned) | self-hosted measurements |
| **Outcome** | one application report (planned v0.1) | success/failure joined to a span |
| **Health** | one event or counter snapshot | drops, failures, unflushed, adapter status |

### 2.2 Relationships

```
Run ──< Span ──< Span (parent)                attribution inherited downward
Span ──< Operation                            implicit model_call span when none is open
Operation ──< Attempt                         1..n; SDK source sees the last attempt only
Attempt ──< Identifier                        provider_object_id · http_request_id · harness ids · proxy ids
Attempt ──< SourceObservation                 one per source that saw it; one is *selected*
SourceObservation ──< Correlation >── SourceObservation   exact · inferred · conflicting · rejected
Attempt ──1 Charge                            potential or confirmed expenditure
Charge ──< CostEstimate ──> RateCard          one current estimate per perspective
Charge ──< BillingFact                        (planned) reconciliation evidence
Span ──< Outcome                              (planned v0.1)
Run ──< Health
```

### 2.3 Identity — six things that are not the same

| Level | Identifier | Minted by | Notes |
|---|---|---|---|
| Application operation | `operation_id` | harness, at hook entry | always present for harness-observed calls |
| Attempt | `attempt_id` | harness or proxy | the SDK hook mints exactly one per operation and labels it `visibility = last_only` |
| Provider response-object id | `provider_object_id` (`chatcmpl-…`, `resp_…`) | provider, in the body | **verified**: `ChatCompletion.id`, `ChatCompletionChunk.id`, `Response.id` (`types/chat/chat_completion.py:196`, `chat_completion_chunk.py:266`, `responses/response.py:68`) |
| HTTP request id | `http_request_id` (`x-request-id` header) | provider edge | **verified**: attached to parsed models as `_request_id` (`_models.py:133–141`, `_response.py:331`); on streams via `stream.response.headers` (`_streaming.py:23–40`) |
| Source observation | `observation_id` | harness | one per source per attempt |
| Charge | `charge_id` | harness | one per attempt |

Identifiers are rows, not columns: `(subject_kind, subject_id, id_kind, namespace, value,
source)`. `id_kind` ∈ `provider_object_id` · `http_request_id` · `harness_operation_id` ·
`harness_attempt_id` · `proxy_request_id` · `otel_span_id`. A unique index on
`(id_kind, namespace, value)` means an identifier value resolves to at most one attempt; two
sources presenting the same one are **exactly** correlated. A response-object id and an HTTP
request id are never compared to each other.

**What the SDK hook can and cannot see (verified).** The retry loop lives in
`_base_client.request()` (`_base_client.py:1017`, `for retries_taken in range(max_retries + 1)`),
below `Completions.create`. Default `max_retries` is 2 (`_constants.py:10`). It retries on
408, 409, 429, ≥500, `x-should-retry: true`, timeouts and connection errors
(`_base_client.py:795–828, 1036–1070`). Each attempt sends `x-stainless-retry-count`
(`_base_client.py:471–472`). No idempotency header is sent for OpenAI by default
(`_idempotency_header = None`, `_base_client.py:394`). Therefore a hook on `create()` observes
one call, receives the **last** attempt's response, and cannot count earlier attempts.
`retries_taken` exists only on raw response objects (`_response.py:58`,
`_legacy_response.py:67`), reachable via `with_raw_response`, not on parsed models.
Whether a failed earlier attempt was billed is **unverifiable** from the client.

**Attempt visibility** on every attempt: `single` (source saw every request — a proxy),
`last_only` (SDK hook), `aggregate` (source reported operation-level totals only, e.g. a
framework callback). An `aggregate` observation creates one synthetic attempt and is never
added to attempt-level usage that exists for the same operation.

### 2.4 Correlation

| Class | Evidence | Effect on totals | Effect on coverage |
|---|---|---|---|
| **exact** | shared `(id_kind, namespace, value)`, or same `harness_operation_id` in-process | one attempt, one charge; one selected observation | counted once |
| **inferred** | same provider+model, start times within `tolerance_ms` (default 2000), usage within `tolerance_tokens` (default 1%) — no shared identifier | **both attempts stay in totals**; the pair is listed under *unresolved potential duplication* with the smaller amount as the possible over-count | reported separately, never subtracted |
| **conflicting** | exact correlation, but usage differs beyond tolerance | one charge; precedence winner selected; loser retained; flagged | counted once, flagged |
| **rejected** | operator marked an inferred pair as distinct | both attempts, no listing | — |
| **confirmed** | operator accepted an inferred pair (`runpeek correlate --accept`) | treated as exact; provenance `operator` | counted once |

Precedence for selection: provider-client hook › proxy/gateway export › framework callback
› OTel span › user-supplied; within a source, `exact` usage over `estimated`. Values are
never averaged. A total is described as *deduplicated* only when every correlation touching
it is exact or confirmed; otherwise the summary prints the over-count bound.

### 2.5 Charges and billing status

A charge is a **potential or confirmed expenditure**, created for every attempt that
reached the provider. Two independent statuses:

`billing_status`
- `expected` — a provider response with a usage block was received; billing is expected.
- `unknown` — an error, timeout, cancellation or incomplete stream; the provider may or
  may not bill. Never assumed either way.
- `confirmed` — matched to a billing fact (planned).
- `not_billed` — a billing fact or provider usage export covering the period shows no
  charge (planned). Never inferred from an HTTP status alone.

`estimation_status`
- `priced` — usage present, rate resolved.
- `unpriced` — usage present, no rate for this model in the perspective's rate card.
- `no_usage` — no usage block; nothing to price.
- `partial` — some usage fields missing (e.g. streamed output count only); amount is a
  lower bound and labelled.

A missing `provider_object_id` or `http_request_id` does not affect either status: the
attempt is keyed by `attempt_id`, priced from its usage, and simply has fewer identifiers
for correlation.

Reconciliation (planned) never edits an estimate. It adds billing facts, sets
`billing_status`, and produces a new estimate revision under a `reconciled` perspective.
Totals still select one current estimate per charge per perspective, so nothing is counted
twice.

### 2.6 Rate cards and estimates

- A **rate card** is immutable: `rate_card_id` (e.g. `openai-list@2026-08-01`), source
  (`list` / `contract`), `effective_from`, `effective_to` (nullable = open), checksum, and
  per-model prices for input / cached input / output / reasoning tokens. Shipped cards are
  data files; contract cards are user files with the same schema.
- **Resolution**: for an attempt, choose the card of the perspective's source whose window
  contains `attempt.started_at` → `rate_resolution = effective_at_execution`. If none,
  apply the perspective's `fallback` (`nearest_earlier` default, `nearest`, or `none`) and
  record `fallback_nearest_earlier` / `fallback_nearest_later` / `none`. Summaries print
  the count of fallback-priced charges. There is no "latest" default.
- **Perspective** (persisted, named):

  ```
  perspective:
    name:        default
    rates:       list | contract | reconciled
    resolution:  effective_at_execution
    fallback:    nearest_earlier | nearest | none
    pin:         <rate_card_id>           # optional, forces one card
    allocation:  none | token_work | wall_time | equal    # planned
    include:     [provider, share]
  ```

- **Estimate key** = hash(charge_id, perspective_id, selected_observation_id, rate_card_id,
  allocation_inputs_hash, calc_version). `UNIQUE(estimate_key)`. Re-running with identical
  inputs is a no-op. Any change to selection, rate card, allocation inputs or calculator
  version produces a new key; the previous revision gets `superseded_by`. The **current**
  estimate per (charge, perspective) is the newest non-superseded revision. Queries print
  which perspective, rate cards and calc version they used.
- Repricing is explicit: `runpeek reprice --perspective P [--pin CARD]`. Nothing reprices
  on read.

### 2.7 Uncertainty dimensions on every estimate

`usage_provenance` (`exact` / `estimated` / `missing`, plus source) · `rate_provenance`
(`list` / `contract` / `statement` / `none`) · `rate_resolution` (above) ·
`allocation_method` · `completeness` (`complete` / `partial`, names the missing field) ·
`billing_status` (inherited from the charge). No single confidence score.

---

## 3. Invariants

**Accounting**
- A1. Cost derives only from charges. Spans and operations carry no cost; their cost is the
  sum over their attempts' charges.
- A2. Under one perspective, each charge contributes exactly one current estimate to any
  total. Perspectives are never summed.
- A3. Unknown is never zero: missing usage → `no_usage`, missing rate → `unpriced`, missing
  billing evidence → `unknown`. Each is a labelled bucket in every summary.
- A4. Estimates are immutable; changes are new revisions with keys; identical inputs are
  idempotent.
- A5. Aggregate (operation-level) usage is never added to attempt-level usage of the same
  operation.

**Identity**
- I1. One `operation_id` per wrapped call; one `attempt_id` per observable provider request.
- I2. Identifier kinds are distinct; only equal `(id_kind, namespace, value)` is exact
  evidence.
- I3. Inferred correlation never removes a charge from totals.
- I4. Source observations are never edited or deleted by correlation or selection.

**Lifecycle**
- L1. The wrapped SDK method is invoked exactly once per application call. This is not a
  claim about HTTP requests (see §2.3).
- L2. A hook failure before the call falls through to one invocation; after the call it
  records health and returns the obtained result or re-raises the obtained exception.
- L3. An attempt's `observed_status` is written only by the hook that observed it. Diagnostic
  reconciliation writes a separate `diagnostic_status`; it never rewrites observed fields
  or timing.
- L4. A start record and a terminal record are separate writes; either may be lost; a
  terminal without a start still creates the attempt with `start_missing = true`.

**Aggregation**
- G1. Attributed share is computed by cost, over priced charges; unpriced and no-usage
  charges are counted separately by operation count.
- G2. A total is labelled *deduplicated* only under §2.4's condition; otherwise it carries a
  possible over-count bound.
- G3. `no observations` is never rendered as `no spend`.

---

## 4. Execution lifecycle

Attempt `observed_status` values and the record that produces them:

| Event | Start record | Terminal record | `observed_status` | `billing_status` |
|---|---|---|---|---|
| hook entry | written (`in_progress`) | — | `in_progress` | — |
| non-stream success | — | usage, ids, timing | `completed` | `expected` |
| provider exception | — | error class, http status if any, ids if any | `provider_error` | `unknown` |
| stream exhausted | — | usage if a usage chunk/`response.completed` arrived, else partial | `completed` (`completeness = partial` if no usage) | `expected` |
| explicit `close()` / context exit before exhaustion | — | chunks so far, `ttft` | `stream_closed` | `unknown` |
| `CancelledError` / `GeneratorExit` / `KeyboardInterrupt` during iteration | — | chunks so far | `cancelled` | `unknown` |
| GC finaliser on an unexhausted stream (best effort) | — | chunks so far | `abandoned` | `unknown` |
| process crash | persisted | never | stays `in_progress` | `unknown` |
| start record dropped, terminal persisted | — | creates attempt, `start_missing = true` | terminal's | terminal's |
| terminal dropped | persisted | — | stays `in_progress` | `unknown` |

The start record is enqueued **before** the SDK call, so an attempt is visible while it
runs. Because the writer is asynchronous, the terminal may reach the store first; the writer
upserts by `attempt_id`, and start fields fill only nulls.

`doctor` (planned v0.1; v0 ships the `in_progress` count in `summary`) assigns
`diagnostic_status`: `stale_in_progress` (run ended, or heartbeat older than
`stale_after`), `start_missing`, `terminal_missing`. `doctor --reconcile` writes
`diagnostic_status` and a reconciliation row (time, rule, command). Reading the database
never changes it.

Streaming (verified): `Stream` exposes `__iter__`, `__enter__/__exit__`, `close()`
(`_streaming.py:48,115,123`); `AsyncStream` exposes `aclose()` (`_streaming.py:233`). Chat
usage arrives in a final chunk only when `stream_options={"include_usage": True}` was sent
(`types/chat/chat_completion_stream_options_param.py:24`); the harness does not inject it by
default because it changes the chunks the application receives. Responses streams carry
usage in `response.completed` (`types/responses/response_completed_event.py:11–20`).

---

## 5. Telemetry loss

| Class | Mechanism | Reported as |
|---|---|---|
| Confirmed dropped | `queue.put_nowait` raised `Full` | `dropped_confirmed` counter in health and in the run's final counters |
| Known unflushed | queue length + in-flight batch at the shutdown deadline (default 5 s) | `unflushed_known` in the run's final counters and one stderr line |
| Persist failure | writer could not commit (locked, disk full, schema mismatch) | `persist_failures` counter; one stderr line per minute; the app continues |
| Unknown loss after crash | run has `started_at`, heartbeats every `heartbeat_s` (default 10), no `ended_at` | `doctor`: "last heartbeat at T; process did not end cleanly; loss unknown. Upper bound under the assumption of steady rate since last heartbeat: N records" — labelled an assumption |

Health records travel through the same queue and can be lost with it. The run's final
counters are written by the writer thread itself after draining, outside the queue, and are
the last write before close; if that write fails, stderr is the only record. Exact crash-loss
counts are not promised. Overhead: counters are plain integers; the heartbeat is one small
row per `heartbeat_s`.

---

## 6. Local-first operation and OSS security

| Concern | Behaviour |
|---|---|
| Network | None required. No telemetry, update check or phone-home. |
| Hot path | read ContextVar, mint ids, enqueue start, invoke once, capture, enqueue terminal. |
| Buffering | bounded queue (default 10,000); one writer thread per process; batches ≤ 500 rows or 250 ms. |
| Overload | drop newest; count; one stderr line per minute. |
| Concurrent writers | one file per host; per-process connection; WAL; `busy_timeout = 5 s`. |
| Store file | created `0600`; directory `.runpeek/` created `0700`; path from `RUNPEEK_DB` or `./.runpeek/runpeek.db`. |
| Dashboard (planned v0.2) | binds `127.0.0.1` by default; non-loopback binding requires `--bind` plus a token printed at start; no enterprise identity in OSS v0–v0.2. |
| Content | prompts and completions are not captured. Opt-in capture (planned) requires a redaction hook and a separately retained table. |
| Identifier-bearing fields | customer ids, span names, attributes are stored as given and can carry sensitive data; pass opaque ids; deletion (planned v0.1) covers them. |
| Launch modes | `runpeek run <cmd>` prepends a `sitecustomize` directory to `PYTHONPATH`. **Gated support** (§9): `python script.py` in v0; `python -m`, `uvicorn`, `gunicorn`, `celery` each require their gate test. **Not covered**: `-S`/`-I`, embedded interpreters, environment-scrubbing launchers, non-Python processes, clients imported before `site` ran. |
| `with_raw_response` | **verified**: `CompletionsWithRawResponse` wraps the *bound* `completions.create` at construction (`resources/chat/completions/completions.py:3187–3195`) and is a `cached_property` (`:72–73`). Covered when the class is patched before the property is first accessed; an already-cached wrapper is not covered and the gate test asserts both cases. |

---

## 7. Worked examples

Rate card for all examples: `openai-list@2026-08-01`, model `m-small`, input $0.40 / M
tokens, output $1.60 / M tokens — **illustrative prices, not a real card**. Perspective
`default` (`rates: list`, `resolution: effective_at_execution`, `fallback: nearest_earlier`).

**E1 — normal priced operation.** `create()` returns 1,000 input / 500 output tokens,
`chatcmpl-a1`, `x-request-id: req-a1`. Records: 1 operation, 1 attempt (`last_only`,
`completed`), 2 identifiers, 1 source observation (selected), 1 charge (`expected`,
`priced`), 1 estimate $0.0012 (`effective_at_execution`). Total: **$0.0012**.

**E2 — usage, no provider request id.** Same usage; the response lacks `id` and the header.
Records identical except 0 identifiers. Charge `expected` / `priced`, $0.0012. Total:
**$0.0012**. Correlation with any later proxy source can only be inferred.

**E3 — two sources, same request.** SDK hook and a proxy export both report `req-a1`.
Records: 1 attempt, 2 source observations, 1 exact correlation, SDK observation selected
(precedence). If the proxy reported 1,010 input tokens: 1% tolerance → within → not
flagged; if 1,200: `conflicting`, SDK value used, both kept. Either way 1 charge. Total:
**$0.0012**, deduplicated.

**E4 — multiple observable attempts.** The app retries once at application level (two
`create()` calls under one `retry_group` span); the first raises 429 after the SDK's own two
internal retries. Records: 2 operations, 2 attempts (`last_only` each). Attempt 1:
`provider_error`, charge `unknown` / `no_usage`, $null. Attempt 2: `completed`, $0.0012.
Internal retries of attempt 1 are invisible (§2.3). Total: **$0.0012**, plus "1 charge with
unknown billing".

**E5 — a stream that never completes.** `stream=True` without `include_usage`; the app
reads 37 chunks and the process is killed. Records: 1 operation, 1 attempt with a start
record and no terminal, `observed_status = in_progress`. Summary: "1 in-progress attempt
(run did not end cleanly)". Charge: `unknown` / `no_usage`, $null. Total contribution:
**$0**, shown as unmeasured, not as free. After `doctor --reconcile`:
`diagnostic_status = stale_in_progress`; observed fields untouched.

**E6 — repricing a historical operation.** E1 ran on 2026-08-15. A new card
`openai-list@2026-09-01` lowers output to $1.20/M. `runpeek summary` still shows $0.0012:
resolution picks the card effective on 2026-08-15. `runpeek reprice --perspective default
--pin openai-list@2026-09-01` creates estimate revision 2 ($0.0010, `rate_resolution =
pinned`), supersedes revision 1, and the header now names the pin. Running the same
command again creates nothing (identical key). Both revisions remain in `explain`.

---

## 8. CLI summary (target output)

```
runpeek · run 7c1e0b  ·  python app.py  ·  ended cleanly in 41.2 s
perspective default  ·  rates list  ·  cards openai-list@2026-08-01  ·  calc 1

OPERATIONS        212 attempts (212 operations)        adapters: openai.chat.completions ✓
  completed       203      provider_error 6      in_progress 3 (terminal record never arrived)

COST              $4.8130  estimated, list price   [possible over-count ≤ $0.0210 from 2 unresolved pairs]
  priced          198 charges   $4.8130
  unpriced          5 charges   model not in rate card: "m-preview" (5)
  no_usage          9 charges   6 errors, 3 in progress
  billing          expected 198 · unknown 14 · confirmed 0

COVERAGE          usage exact 198 / estimated 0 / missing 14   ·   pricing 198 / 203 priced   ·   capture: not measurable
ATTRIBUTION       by cost: attributed 81.4% · job_only 6.1% · unattributed 12.5% ($0.6020, 27 ops outside runpeek.job)

BY CUSTOMER                 ops     cost      share
  acme                       96   $2.2110    45.9%
  globex                     58   $1.4470    30.1%
  initech                    31   $0.2590     5.4%
  (unattributed)             27   $0.6020    12.5%

BY MODEL                    ops     in tok     out tok   cached     cost
  m-small                   176    1.21 M      412 k       0       $3.1430
  m-large                    22    0.30 M       88 k     40 k      $1.6700
  m-preview                   5       —           —        —       unpriced

TELEMETRY         dropped 0 · unflushed 0 · persist failures 0
```

---

## 9. First milestone, gates, repository, steps, tests

### 9.1 Milestone M1 — one path end to end

OpenAI Python SDK · `chat.completions.create` · synchronous · non-streaming · launched as
`runpeek run python script.py` · `job()` attribution · SQLite store · `summary`, `events`,
`export --format jsonl`.

Why this path: it is the most common call in existing Python applications, its lifecycle is
the simplest (one terminal record), and it exercises every accounting table. Nothing about
the accounting model is simplified for it.

### 9.2 Support gates

A gate is claimed only when its tests pass in CI. The README lists gates by status.

| Gate | Adds | Required tests |
|---|---|---|
| G1 | chat.completions, sync, non-stream (M1) | E1, E2, E3 (simulated second source), E4, E6; exactly-once under hook failure before and after the call; `with_raw_response` cached before and after patch; identifiers stored by kind |
| G2 | sync streaming | finalisation on exhaustion (with and without `include_usage`), `close()`, context exit, exception, `GeneratorExit`; TTFT recorded; E5 via subprocess kill |
| G3 | async, non-stream and stream | context inheritance across `create_task`; `aclose()`; `CancelledError` → `cancelled` |
| G4 | `responses.create` sync/async/stream | usage from `response.completed`; `resp_` ids |
| G5 | launch modes: `python -m`, `uvicorn`, `gunicorn`, `celery` | one subprocess test per mode asserting records from a worker; `-S` asserted **uncovered** |
| G6 | thread carrier | `wrap()` restores context in `ThreadPoolExecutor`; unwrapped thread → `unattributed` |

v0 ships when G1 passes; the one-week target includes G2 and G3 if their gates pass, and
they are omitted from the README otherwise.

### 9.3 Repository

```
runpeek/
  pyproject.toml            zero runtime deps; dev: pytest, openai, httpx
  README.md                 gates table with status; five-minute path
  docs/DESIGN.md            this document
  src/runpeek/
    __init__.py             job, install, inject, extract, wrap
    context.py              Attribution ContextVar; carrier
    ids.py                  id minting; identifier kinds
    model.py                record dataclasses; SCHEMA_VERSION
    schema.sql              tables, indexes, views
    store.py                Sink protocol; SQLiteSink; writer thread; run/heartbeat
    accounting.py           charges, selection, correlation (exact only in v0), estimates
    rates.py  rates/*.json  rate-card loading, resolution, fallback
    perspective.py          perspective config
    summary.py              queries behind `summary`
    cli.py                  run, summary, events, export
    bootstrap.py            install(); env-driven config
    _boot/sitecustomize.py
    instrumentation/
      __init__.py           registry; idempotent patching; health
      openai_chat.py        G1 (G2/G3 extend this file)
  tests/                    one file per gate plus store/accounting/context
  bench/                    §10 scenarios
```

### 9.4 Ordered steps

1. `schema.sql`, `model.py`, `store.py` with writer thread, run record, heartbeat, final
   counters, stderr fallbacks. Tests: upsert-by-attempt ordering, drop counting, shutdown
   deadline.
2. `context.py`, `ids.py`. Tests: nesting, inheritance, carrier round-trip, thread
   non-inheritance.
3. `rates.py`, `perspective.py`, `accounting.py` (charges, estimate keys, idempotent
   recompute, exact correlation + selection). Tests: A1–A5, E6, unknown → labelled buckets.
4. `instrumentation/openai_chat.py` G1 against the real SDK with `httpx.MockTransport`.
   Tests: G1 list.
5. `cli.py` `run` + `sitecustomize`; `summary`, `events`, `export`. Test: end-to-end
   subprocess producing the §8 sections.
6. README with gate table; `bench/` B1–B3.
7. G2, G3 — each merged only with its gate green.

### 9.5 Acceptance for M1

- All G1 tests green; `tsc`-equivalent hygiene: `ruff`, `mypy --strict` on `src/`.
- `runpeek run python examples/basic.py` against the mock transport prints a summary whose
  cost equals the hand-computed value from the example rate card to the cent.
- An app whose hook raises before the call still receives its response, and the store shows
  the health event.
- Killing the app mid-run leaves a readable database (`PRAGMA integrity_check = ok`) with the
  run lacking `ended_at` and `summary` saying so.

---

## 10. Benchmarks — design targets

Conditions: pinned CI runner (named in the README), Python 3.12, mock transport with a fixed
1 KB response body, rate card loaded, `queue = 10,000`, `batch = 500 / 250 ms`. Storage is
measured as (db + wal after `wal_checkpoint(TRUNCATE)`) size delta ÷ completed operations,
including all associated rows and indexes.

| Scenario | Measure | Target | Acceptance |
|---|---|---|---|
| B1 ingest only, 1 process | hook overhead p50 / p99; ops/s | < 50 µs / < 500 µs; ≥ 5,000/s | zero drops at 5,000/s |
| B2 ingest + costing + `summary` every 5 s, 1 process | ops/s; summary latency at 1 M ops | ≥ 3,000/s; < 2 s | zero drops |
| B3 storage | bytes per completed operation, all tables + indexes + WAL | ≤ 1.5 KB | measured, published |
| B4 multi-process, 16 writers, one file | committed events vs sent | 100% committed; no `SQLITE_BUSY` surfaced | — |
| B5 shutdown | clean exit with 10,000 queued → committed within deadline; `kill -9` mid-batch → loss vs bound | ≤ 5 s; loss ≤ queue + 1 batch | `integrity_check = ok` |
| B6 streaming, completed vs never-completed | finalisation latency; `in_progress` count after kill | terminal within 5 ms of last chunk; count equals killed streams | — |

All numbers are targets until `bench/` prints them; the README publishes measurements, not
targets.

---

## 11. Comparisons (planned v0.2)

Descriptive comparison (cost, tokens, latency, error rate, incomplete rate) is always
allowed between two runs or periods. An **efficiency verdict** (cost per successful outcome
improved or regressed) is printed only when all of the following hold, and otherwise the
unmet conditions are listed:

- same `workload_key` and `workload_version` on both sides (`job(workload=…)` or
  `compare --workload`);
- outcome definition id equal; outcome coverage ≥ 95% of operations on both sides;
- n ≥ 30 operations per side;
- 100% of charges priced on both sides, or equal pricing coverage;
- the only differing configuration keys are the ones declared with `--changed`;
- usage provenance `exact` on both sides.

This is a table and a checklist, not an experimentation platform.

---

## 12. Deferred

Anthropic and other clients · Responses API (G4) · server and worker launch modes (G5) ·
explicit `span()`, `outcome()`, `trace`, `explain`, `doctor`, retention and deletion (v0.1) ·
inferred correlation and `correlate --accept/--reject` (v0.1) · comparisons and dashboard
(v0.2) · OTel receiver and proxy/gateway ingestion (v0.3) · resource samples, `share`
charges and allocation (v1) · billing facts and reconciliation (later) · content capture
with redaction (later) · pseudonymised export (later).

A note on reuse: an existing TypeScript allocator in the `aluminatai-landing` repository
(`lib/request-attribution.ts`, inspected 2026-09-01) implements token-work allocation with
idle capacity never charged to a request; whether any of it is ported is a v1 decision. No
other legacy component has been assessed for reuse in this design.

---

## 13. Open decisions

1. **PyPI name.** `runpeek` is registered on PyPI at 0.4.1 (`pip index versions`, verified
   2026-09-09). Publishing this project under that name replaces it; a review of existing
   users is required first. Not blocking for M1, which is installable from a git checkout.
2. **Repository location** and **license** — either choice is compatible with this design.
