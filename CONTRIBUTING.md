# Contributing to RunPeek

Thanks for looking. RunPeek is small on purpose: a local-first harness whose
accounting has to be right before it is convenient. Contributions that keep
that property are welcome.

## Set up

```bash
git clone <your checkout> runpeek && cd runpeek
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest            # offline; uses the real openai SDK with a mock transport
ruff check src tests examples
mypy
```

No provider credentials are needed for any test. Please keep it that way:
tests that need a network or a paid call will not be merged.

## Ground rules that reviewers apply

- **Unknown is never zero.** Missing usage, an unknown model price, an error
  or a timeout produce a labelled unknown, not `$0`.
- **Nothing content-like is stored.** Prompts, completions, tool inputs and
  outputs, commands and file contents never reach the store, exports,
  findings or error messages. Fingerprints are keyed HMACs and are treated
  as sensitive.
- **Fail open.** Instrumentation never changes application behaviour: the
  wrapped SDK call runs exactly once and its result or exception is passed
  through untouched.
- **Claims match tests.** A surface (SDK method, transcript version, launch
  mode) is "supported" only when a test exercises it. Say "not tested" otherwise.
- **Findings are potential inefficiencies**, phrased with what was observed,
  the evidence, a next step and the limitation needed to read them.

## Adding a source adapter

Read `docs/DESIGN.md` §1–2 and `docs/AGENT_SOURCES.md`. An adapter emits the
normalised events in `runpeek/agents/events.py` and nothing else; persistence,
checkpoints and diagnostics are shared. Include sanitised fixtures (see
`tests/agent_fixtures.py`) and a structural note on the source's format and
version you built against.

## Adding a rate card

Rate cards are immutable and dated (`src/runpeek/rates/*.json`). Never edit a
shipped card; add a new one with the retrieval URL and date, and set
`effective_from` to the verification date unless you have evidence of when
the prices began. See `docs/PRICING.md`.

## Pull requests

- One change per PR; include tests; keep `ruff` and `mypy --strict` clean.
- Update `CHANGELOG.md` under *Unreleased*.
- Do not add dependencies to the runtime package (it has none).
- Contributions are accepted under the Apache License 2.0 (see `LICENSE`).
