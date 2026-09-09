"""An OpenAI client that never touches the network.

Uses the real ``openai`` SDK with an ``httpx.MockTransport``, so everything
the harness observes — response parsing, usage blocks, ids, headers, error
classes — is exactly what a real call would produce. No credentials needed.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx
from openai import OpenAI

Scenario = dict[str, Any]


def chat_json(
    *,
    model: str = "gpt-4.1-mini",
    prompt_tokens: int | None = 1000,
    completion_tokens: int | None = 500,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    object_id: str | None = "chatcmpl-example",
    content: str = "ok",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
    if object_id is not None:
        body["id"] = object_id
    if prompt_tokens is not None or completion_tokens is not None:
        usage: dict[str, Any] = {
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
            "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
        }
        if cached_tokens is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
        if reasoning_tokens is not None:
            usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
        body["usage"] = usage
    return body


def make_client(handler: Callable[[httpx.Request], httpx.Response], *, max_retries: int = 0) -> OpenAI:
    transport = httpx.MockTransport(handler)
    return OpenAI(
        api_key="offline-example-key-not-real",
        base_url="http://mock.invalid/v1",
        http_client=httpx.Client(transport=transport),
        max_retries=max_retries,
    )


def scripted_client(scenarios: Iterable[Scenario], *, max_retries: int = 0) -> OpenAI:
    """Each call consumes the next scenario. A scenario is either
    ``{"json": {...}, "request_id": "req-…", "status": 200}`` or
    ``{"raise": httpx.ReadTimeout}``."""
    it = itertools.cycle(list(scenarios))

    def handler(request: httpx.Request) -> httpx.Response:
        sc = next(it)
        if "raise" in sc:
            raise sc["raise"]("simulated", request=request)
        headers = {"content-type": "application/json"}
        if sc.get("request_id"):
            headers["x-request-id"] = sc["request_id"]
        return httpx.Response(sc.get("status", 200), headers=headers, content=json.dumps(sc["json"]).encode())

    return make_client(handler, max_retries=max_retries)
