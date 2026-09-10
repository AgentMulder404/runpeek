"""The five-minute path, offline.

    runpeek run python examples/basic.py

A small "support assistant" that serves three customers. The OpenAI client is
real; its transport is a local mock, so no key and no network are needed.
Attribution comes from ``runpeek.job``; the harness sees every call through
the SDK, exactly as it would in your own application.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _mock_openai import chat_json, scripted_client  # noqa: E402

import runpeek  # noqa: E402

client = scripted_client(
    [
        {"json": chat_json(prompt_tokens=1200, completion_tokens=300, object_id="chatcmpl-1"), "request_id": "req-1"},
        {"json": chat_json(prompt_tokens=900, completion_tokens=450, cached_tokens=600, object_id="chatcmpl-2"),
         "request_id": "req-2"},
        {"json": chat_json(model="gpt-4o", prompt_tokens=2000, completion_tokens=800, object_id="chatcmpl-3"),
         "request_id": "req-3"},
        {"json": {"error": {"message": "rate limited", "type": "rate_limit"}}, "status": 429, "request_id": "req-4"},
        {"json": chat_json(model="gpt-4.1-mini", prompt_tokens=700, completion_tokens=350, object_id="chatcmpl-5"),
         "request_id": "req-5"},
        {"json": chat_json(model="acme-preview-1", prompt_tokens=500, completion_tokens=200, object_id="chatcmpl-6"),
         "request_id": "req-6"},
        {"raise": httpx.ReadTimeout},
        {"json": chat_json(prompt_tokens=400, completion_tokens=120, object_id=None), "request_id": None},
    ]
)


def ask(model: str, text: str) -> str | None:
    try:
        r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": text}])
        return r.choices[0].message.content
    except Exception as exc:  # the app decides what an error means; the harness only records it
        print(f"  provider error: {type(exc).__name__}")
        return None


def main() -> None:
    print("support assistant (offline demo)")
    with runpeek.job(customer="acme", job="triage"):
        ask("gpt-4.1-mini", "Summarise ticket #101")
        ask("gpt-4.1-mini", "Summarise ticket #102")
        with runpeek.job(job="escalation"):  # inherits customer=acme
            ask("gpt-4o", "Draft an escalation for ticket #102")
    with runpeek.job(customer="globex", job="triage"):
        ask("gpt-4.1-mini", "Summarise ticket #201")  # 429
        ask("gpt-4.1-mini", "Summarise ticket #202")
    with runpeek.job(customer="initech", job="triage"):
        ask("acme-preview-1", "Summarise ticket #301")  # model not in the rate card
        ask("gpt-4.1-mini", "Summarise ticket #302")  # timeout
    ask("gpt-4.1-mini", "warm-up ping")  # outside any job: unattributed, and no request id
    print("done")


if __name__ == "__main__":
    main()
