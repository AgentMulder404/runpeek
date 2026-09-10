# RunPeek
### by NemulAI

**See where your AI spends time and tokens.**

RunPeek observes supported AI applications and Claude Code sessions locally,
attributes estimated model costs, and highlights repeated failures and
repeated work.

It watches two things: the OpenAI Python SDK calls your own application makes
(`runpeek run`), and the Claude Code sessions in a project (`runpeek watch`).
It helps you answer *which customer or job caused these model calls*, *what
did they consume*, *what would that cost at list prices*, and *where did the
agent repeat itself*. Collection and analysis run entirely on your machine —
no account, no service, no uploads, no runtime dependencies.

RunPeek is not a model, an agent framework, or a gateway. Its diagnostics are
deterministic local analysis of what a supported source already records.

**Sample output** (rendered from a test fixture, not a real session):

```
05:03  REPEATED READ
       package.json was read 4 times in 2 minutes.
       No edit to that file was observed between reads.
       Repeated billing cannot be determined.

       Evidence: runpeek session 719c4032

05:04  REPEATED FAILURE
       A python3 command failed 4 times consecutively over 46 seconds.
       No edit or write was observed between the failures.
       Check the error before repeating the command.

       Evidence: runpeek session 719c4032

05:04  TURN FINISHED
       51 seconds · 8 tool calls (4 failed) · 8 model calls
       API-equivalent estimate: $0.115
       2 potential inefficiencies to review · runpeek session 719c4032
```

## Install

RunPeek is not on PyPI yet. Install from a checkout:

```bash
git clone <path-or-url-of-this-repository> runpeek && cd runpeek
python -m venv .venv && . .venv/bin/activate
pip install -e .            # runtime: no dependencies
pip install -e ".[dev]"     # adds the openai SDK, httpx, pytest, ruff, mypy — needed for the offline demo and tests
```

Python 3.10+.

## Quickstart A — watch Claude Code

```bash
runpeek watch --project /path/to/project
```

Keep using Claude Code normally in that project. The watcher first loads
recent history (transcripts modified in the last 7 days; `--history all` or
`--history none`) and summarises any historical findings without replaying
them as live events. Then it prints only meaningful events: new sessions,
turns starting and finishing, potential inefficiencies, and collection
problems. Ctrl-C stops the watcher; your Claude session keeps running.

Review afterwards:

```bash
runpeek sessions --project /path/to/project     # started, tool calls, errors, model calls, items to review
runpeek findings --project /path/to/project     # potential inefficiencies, newest first
runpeek session <session-id>                     # one session: activity, items with evidence, usage and estimate
```

`--project` defaults to the current directory; `--all-projects` covers every
project Claude Code has transcripts for. If the filter finds nothing, RunPeek
says so and names the projects it has collected.

The Claude Code transcript adapter is **experimental**: it reads
`~/.claude/projects/<project>/*.jsonl` (and `…/<session>/subagents/*.jsonl`),
whose format is undocumented. It was built against transcripts written by
Claude Code 2.1.x (CLI 2.1.258 verified) and marks other writer versions as
`unknown_version`, parsed best-effort.

## Quickstart B — observe a Python AI application

```bash
export OPENAI_API_KEY=…        # your application's own credentials; RunPeek never reads or stores them
runpeek run python app.py
```

Optionally attribute calls from inside your code:

```python
import runpeek

with runpeek.job(customer="acme", job="support_ticket"):
    client.chat.completions.create(...)     # observed and attributed
```

Nested `job()` inherits `customer`; calls outside any `job()` are recorded as
"No customer tag" — a real row with a real estimate, not a missing one.
Context follows `await` and `asyncio.create_task`; for threads use
`runpeek.wrap(fn)`, across processes `runpeek.inject()` / `runpeek.extract()`.

When the application exits, RunPeek prints a summary. Later:

```bash
runpeek summary                     # latest run (add --verbose for the full accounting ledger)
runpeek events --last 20            # per-call detail
runpeek export --format jsonl --out run.jsonl
runpeek reprice --pin openai-list@2026-09-09   # price under a separate pinned perspective
```

**Supported SDK surface:** `openai` Python SDK, `client.chat.completions.create`,
synchronous, non-streaming — tested against `openai==2.44.0`. Streaming calls
are recorded as observed-but-unmeasured (`stream=True`, no usage); `AsyncOpenAI`,
the Responses API and other providers are not patched. `runpeek run` works for
commands that start a CPython interpreter which processes `site` and inherits
the environment (`python script.py` is tested; `-S`/`-I` and embedded
interpreters are not covered). If you cannot use `runpeek run`, call
`runpeek.install()` before importing the provider client.

### Offline demo (no provider account)

```bash
runpeek run python examples/basic.py
```

`examples/basic.py` drives the real `openai` client through a local mock
transport: three customers, an unknown model, a rate-limit error and a
timeout. It needs the `[dev]` extras; the RunPeek runtime itself has no
dependencies.

**Sample output** (from that demo):

```
RUNPEEK / RUN COMPLETE

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
Details: runpeek events · Full accounting: runpeek summary --verbose
```

## What RunPeek helps you find

- Which customer or job the model calls belonged to, and what they consumed.
- Calls that could not be priced (unknown model) or measured (errors,
  streaming, missing usage) — shown beside the estimate, never folded into it.
- In Claude Code sessions: the same command failing again and again with no
  edit between attempts; the same file read repeatedly without an observed
  edit; a tool erroring across many different inputs; one action looping
  tightly. Each is a *potential* inefficiency with its evidence, a next step,
  and the limitation needed to read it correctly.

## How it works

```
 Python app ──► openai SDK ──► provider        Claude Code ──► ~/.claude/projects/<proj>/*.jsonl
      │  runpeek run: one patched method,                          │  runpeek watch: read-only polling,
      │  exactly-once call, fail-open hooks                        │  checkpoints, no duplicates
      ▼                                                            ▼
 operations · attempts · observations           sessions · turns · tool calls · usage
      └────────────► SQLite ./.runpeek/runpeek.db ◄────────────────┘
                       accounting: charges → dated rate cards → estimates
                       diagnostics: repeated failure · repeated read · retry loop
          runpeek summary · events · export     runpeek sessions · session · findings
```

More in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the full design in
[`docs/DESIGN.md`](docs/DESIGN.md).

## Supported integrations

| Integration | Status | Notes |
|---|---|---|
| OpenAI Python SDK — `chat.completions.create`, sync, non-streaming | **Supported, tested** | `openai==2.44.0`; exactly-once, fail-open |
| OpenAI — streaming | Observed, not measured | recorded as `unsupported_stream`, no usage, no cost |
| OpenAI — `AsyncOpenAI`, Responses API | Not supported | not patched; invisible |
| Claude Code transcripts (2.1.x) | **Supported, experimental** | undocumented format; version-gated adapter |
| Claude Code source-reported cost | Not available | only in Claude Code's OpenTelemetry export, which RunPeek does not consume |
| Codex | Not supported | capability check and adapter path in [`docs/AGENT_SOURCES.md`](docs/AGENT_SOURCES.md) |
| Other providers, raw HTTP, other languages | Not supported | |
| Dashboards, outcome tracking, run comparison, GPU accounting | Not built | see roadmap |

## What the numbers mean

- **Tokens** are reported by the source — the SDK's usage block, or the usage
  Claude Code writes into its transcripts — not independently audited.
- **Estimated cost** is a calculation from those tokens and a dated list-price
  table ([`docs/PRICING.md`](docs/PRICING.md)). It is *not* your subscription
  charge, quota usage, a reconciled bill, or a saving. For Claude Code it is
  labelled "API-equivalent estimate" every time it appears.
- **Unknown stays unknown.** A call with no usage, an unknown model, an error
  or a timeout is counted and shown as unpriced or unknown, never as `$0`.
- **Capture coverage** — how much AI activity RunPeek did not see — is not
  measurable without an independent source. A run with no observations says
  "this does not mean none happened".
- **Findings are potential inefficiencies.** A repeated read shows a file was
  requested again; whether its contents were billed again is not observable.
  Only Edit/Write tool calls count as an observed change; command side effects
  and edits outside the agent are invisible.
- **Historical vs live.** The watcher summarises history at startup and marks
  live events with their transcript timestamps; silence is never treated as a
  stalled agent.
- **Supported calls only.** RunPeek watches the surfaces listed above, not
  every AI tool on your machine.

## Privacy and local storage

Everything lives in `./.runpeek/runpeek.db` (override: `--db`, `RUNPEEK_DB`),
created `0600`. Stored: ids, timestamps, tool names, relative paths / program
names / hostnames, token counts, model names, labels you set, and keyed
fingerprints of tool arguments. Never stored: prompts, completions, tool
inputs or outputs, commands, file contents, credentials. `runpeek export`
writes the stored tables as JSONL with exact monetary values. Details and the
fingerprint caveats: [`docs/PRIVACY.md`](docs/PRIVACY.md).

## Known limitations

- One SDK surface and one agent source; see the matrix above.
- Claude Code transcript format is undocumented; a future Claude Code release
  may change it. Affected sessions are marked, not silently misread.
- Turn durations come from the transcript's own end-of-turn record; subagent
  transcripts have none, so their turns show no duration.
- The recorded command line for `runpeek run` is redacted heuristically
  (inline programs, secret-looking values, URLs, payloads); it is not a
  guarantee.
- No benchmarks are published yet; performance targets in `docs/DESIGN.md`
  are targets.
- The real-provider smoke test (`examples/real_openai.py`) has not been run by
  the maintainers; it needs your own key and costs a fraction of a cent.

## Development and tests

```bash
pip install -e ".[dev]"
pytest                          # offline; real openai SDK over httpx.MockTransport
ruff check src tests examples
mypy                            # strict
python -m build                 # sdist + wheel
```

The test suite covers exactly-once and fail-open hooks, fractional-cent
pricing, cached/reasoning token handling, identity and correlation, idempotent
estimates and repricing, bounded queue and shutdown, transcript ingestion
(partial lines, truncation, rotation, restarts, concurrent sessions),
diagnostics true/false positives, privacy of stored rows and exports, the
terminal policy, legacy-name migration, and every command the UI prints.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Please keep tests offline and keep
unknowns unknown. Security notes: [`SECURITY.md`](SECURITY.md).

## Roadmap (short, not a promise)

- Streaming usage (`stream_options.include_usage`) and async client support,
  each behind its own tested gate.
- Outcomes (`runpeek.outcome()`), cost per successful task, and run-to-run
  comparison with a strict comparability check.
- A Codex adapter once its session format or a documented interface is pinned.
- Optional OpenTelemetry receiver for Claude Code's source-reported cost.

## Migration from the NemulAI harness

Names changed; data did not. `runpeek` replaces `nemulai` for the CLI and
import; `RUNPEEK_*` replaces `NEMULAI_*` (legacy names still honoured); an
existing `./.nemulai/nemulai.db` is used in place with a notice. Details:
[`docs/MIGRATION.md`](docs/MIGRATION.md).

## License

**Not yet licensed for public distribution.** No license file has been added;
the maintainers intend to choose one (Apache-2.0 is recommended) before the
first public release. Until then, all rights are reserved by the authors.
