# Pricing and estimates

RunPeek never sees a bill. Every dollar it shows is a **calculation from
token counts and a dated list-price table**, labelled as such wherever it
appears.

## Rate cards

Shipped in `src/runpeek/rates/`, immutable once released:

| Card | Provider | Retrieved from | Retrieved | `effective_from` |
|---|---|---|---|---|
| `openai-list@2025-08-01` | OpenAI | transcription, original release | — | 2025-08-01 |
| `openai-list@2026-09-09` | OpenAI | developers.openai.com/api/docs/pricing | 2026-09-09 | 2026-09-09 (verification date, not evidence of when prices began) |
| `anthropic-list@2026-09-09` | Anthropic | platform.claude.com/docs/en/about-claude/pricing | 2026-09-09 | 2026-09-09 (as above) |

Fields per model: `input`, `cached_input` (cache-read price), `output`, and
for Anthropic `cache_write_5m` / `cache_write_1h`. Model lookup is exact name
or an explicit alias; **unknown models are reported unpriced, never $0**.

Add your own (contract rates, newer models) with `--rate-card file.json`; the
schema is the shipped files'. Never edit a shipped card — add a dated one.

## Resolution

For each call, the card of the requested source (list by default) whose
effective window contains the call's start time is used; if several cover it,
the newest `effective_from` wins, so older estimates keep their card and
value. If none covers it, the fallback (`nearest_earlier` by default) is used
and the estimate records `rate_resolution = fallback_*`; summaries count
fallback-priced calls.

## Token semantics

OpenAI: `cached_tokens` is a subset of `prompt_tokens`; `reasoning_tokens` a
subset of `completion_tokens` (verified against the SDK's usage types). Cost =
`(prompt − cached) × input + cached × cached_input + completion × output`.

Anthropic (Claude Code transcripts): `input_tokens`, `cache_creation` split
into 5m/1h writes, `cache_read_input_tokens`, `output_tokens` — each priced at
its own rate. Web-search requests are counted, not priced.

## Money

Integer nanodollars in the store, exact `Decimal` arithmetic, half-even
rounding only at the final nanodollar. Displays never round a small positive
amount to `$0.00` (`<$0.01`).

## Perspectives and repricing (harness)

Estimates live under a *perspective* (rate source, resolution rule, optional
pin). `runpeek reprice --pin <card>` creates estimates under a separate pinned
perspective; the default perspective's history is untouched. Re-running with
identical inputs creates nothing (estimate keys are content hashes).

## What this is not

- Not your subscription charge or quota usage (Claude Code).
- Not a reconciled or verified provider bill (no invoice is imported).
- Not a saving. A finding never carries a dollar figure it cannot support.
