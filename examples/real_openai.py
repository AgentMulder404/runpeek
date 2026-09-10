"""Real-provider smoke test. Costs a fraction of a cent.

    export OPENAI_API_KEY=sk-...
    runpeek run python examples/real_openai.py

Two tiny non-streaming chat completions against a real model, one attributed
and one not, plus one streaming call to show it is recorded as unsupported
rather than silently priced. The key is read by the openai SDK from the
environment; the harness never reads, stores or transmits it.
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI

import runpeek

MODEL = os.environ.get("RUNPEEK_SMOKE_MODEL", "gpt-4.1-nano")

if not os.environ.get("OPENAI_API_KEY"):
    sys.exit("set OPENAI_API_KEY to run the real-provider smoke test")

client = OpenAI()

with runpeek.job(customer="smoke-test", job="hello"):
    r = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=5
    )
    print("attributed:", r.model, r.usage and r.usage.total_tokens, "tokens")

r = client.chat.completions.create(
    model=MODEL, messages=[{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=5
)
print("unattributed:", r.model, r.usage and r.usage.total_tokens, "tokens")

with runpeek.job(customer="smoke-test", job="stream"):
    chunks = list(
        client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5, stream=True,
        )
    )
    print("stream:", len(chunks), "chunks (expected in summary as unsupported_stream, no cost)")
