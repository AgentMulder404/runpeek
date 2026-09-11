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
| `openai-list@2026-09-10` | OpenAI | developers.openai.com/api/docs/pricing | 2026-09-10 | 2026-09-10 — same prices as the 09-09 card plus `gpt-5.5` (5.00 / 0.50 / 30.00) |
| `anthropic-list@2026-09-09` | Anthropic | platform.claude.com/docs/en/about-claude/pricing | 2026-09-09; re-verified equal 2026-09-10 | 2026-09-09 (as above) |

Verification log: on 2026-09-10 every model on both OpenAI cards and the
Anthropic card was compared against the live page and found equal; `gpt-5.5`
was the only priced model missing and was added in a new dated card. Nothing
is backdated: usage of `gpt-5.5` (or any model) before its verification date
resolves to the card effective then and is reported unpriced if that card
does not carry it. `runpeek work show --pin <card>` gives a view priced under
a chosen card for that card's provider, labelled, without touching the stored
estimates.

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

Anthropic (Claude Code transcripts): `input_tokens` (already excludes cached),
`cache_creation` split into 5m/1h writes, `cache_read_input_tokens`,
`output_tokens` — each priced at its own rate. Web-search requests are
counted, not priced.

Codex records (OpenAI): `input_tokens` includes `cached_input_tokens`;
`output_tokens` includes `reasoning_output_tokens`. The adapter stores
uncached input, cache read and output separately (reasoning kept as
information), so the same formula applies: `uncached × input + cached ×
cached_input + output × output`. `cache_write_input_tokens` was 0 in every
inspected record; a non-zero value would be priced at the input rate (OpenAI
does not price cache writes separately).

Usage in agent records is **per request** after normalisation: Claude Code
usage is keyed by `message.id` (streaming writes several entries per
response); Codex usage is the difference of consecutive cumulative totals
(see `AGENT_SOURCES.md`). Neither source reports cost.

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
