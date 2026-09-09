from __future__ import annotations

import json

import httpx
import openai
import pytest

import nemulai
from conftest import Harness, chat_json, client_for, make_client, one, rows
from nemulai.instrumentation import openai_chat

MSG = [{"role": "user", "content": "hi"}]


def test_sdk_version_is_the_verified_one() -> None:
    assert openai.__version__ == "2.44.0", "adapter claims are verified against 2.44.0"


def test_normal_priced_call(harness: Harness) -> None:
    client = client_for(chat_json(model="gpt-4.1-mini", prompt_tokens=1000, completion_tokens=500,
                                  object_id="chatcmpl-a1"), request_id="req-a1")
    with nemulai.job(customer="acme", job="triage"):
        r = client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    assert r.choices[0].message.content == "ok"
    conn = harness.account()
    att = one(conn, "SELECT * FROM attempts")
    assert att["observed_status"] == "completed" and att["visibility"] == "last_only"
    assert att["model_served"] == "gpt-4.1-mini" and att["latency_ms"] >= 0
    op = one(conn, "SELECT * FROM operations")
    assert op["customer_id"] == "acme" and op["attribution_state"] == "attributed"
    ids = {(r["id_kind"], r["value"]) for r in rows(conn, "SELECT id_kind, value FROM identifiers")}
    assert ("provider_object_id", "chatcmpl-a1") in ids
    assert ("http_request_id", "req-a1") in ids
    est = one(conn, "SELECT * FROM cost_estimates WHERE current = 1")
    assert est["estimation_status"] == "priced"
    assert est["amount_nanos"] == 1_200_000  # 1000×0.40 + 500×1.60 per M
    assert est["rate_resolution"] == "effective_at_execution"
    assert est["usage_provenance"] == "exact" and est["rate_provenance"] == "list"
    assert one(conn, "SELECT billing_status FROM charges")["billing_status"] == "expected"


def test_cached_and_reasoning_are_subsets_not_extra_charges(harness: Harness) -> None:
    client = client_for(chat_json(model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500,
                                  cached_tokens=200, reasoning_tokens=100))
    client.chat.completions.create(model="gpt-4o-mini", messages=MSG)
    conn = harness.account()
    est = one(conn, "SELECT amount_nanos, detail FROM cost_estimates")
    # (1000-200)×0.15 + 200×0.075 + 500×0.60 per M = 0.000120 + 0.000015 + 0.000300
    assert est["amount_nanos"] == 435_000
    d = json.loads(est["detail"])
    assert d["billable_input"] == 800 and d["cached_input"] == 200
    obs = one(conn, "SELECT reasoning_tokens FROM source_observations")
    assert obs["reasoning_tokens"] == 100  # recorded, never priced separately


def test_missing_ids_still_priced(harness: Harness) -> None:
    client = client_for(chat_json(prompt_tokens=10, completion_tokens=10, object_id=None), request_id=None)
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    assert one(conn, "SELECT COUNT(*) FROM identifiers WHERE subject_kind = 'attempt'")[0] == 0
    est = one(conn, "SELECT estimation_status, amount_nanos FROM cost_estimates")
    assert est["estimation_status"] == "priced" and est["amount_nanos"] == 4_000 + 16_000


def test_unknown_model_is_unpriced_not_zero(harness: Harness) -> None:
    client = client_for(chat_json(model="acme-preview", prompt_tokens=10, completion_tokens=10))
    client.chat.completions.create(model="acme-preview", messages=MSG)
    conn = harness.account()
    est = one(conn, "SELECT estimation_status, amount_nanos, detail FROM cost_estimates")
    assert est["estimation_status"] == "unpriced" and est["amount_nanos"] is None
    assert "acme-preview" in json.loads(est["detail"])["reason"]


def test_missing_usage_block(harness: Harness) -> None:
    client = client_for(chat_json(prompt_tokens=None, completion_tokens=None))
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    assert one(conn, "SELECT usage_source FROM source_observations")["usage_source"] == "missing"
    est = one(conn, "SELECT estimation_status, amount_nanos FROM cost_estimates")
    assert est["estimation_status"] == "no_usage" and est["amount_nanos"] is None
    assert one(conn, "SELECT billing_status FROM charges")["billing_status"] == "unknown"


def test_provider_error_records_potential_charge(harness: Harness) -> None:
    client = client_for({"error": {"message": "boom", "type": "server_error"}}, status=500, request_id="req-err")
    with pytest.raises(openai.InternalServerError):
        client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    att = one(conn, "SELECT * FROM attempts")
    assert att["observed_status"] == "provider_error"
    assert att["error_class"] == "InternalServerError" and att["http_status"] == 500
    assert one(conn, "SELECT value FROM identifiers WHERE id_kind = 'http_request_id'")["value"] == "req-err"
    assert one(conn, "SELECT billing_status FROM charges")["billing_status"] == "unknown"
    assert one(conn, "SELECT estimation_status FROM cost_estimates")["estimation_status"] == "no_usage"


def test_timeout_records_potential_charge(harness: Harness) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = make_client(handler, max_retries=0)
    with pytest.raises(openai.APITimeoutError):
        client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    att = one(conn, "SELECT observed_status, error_class, http_status FROM attempts")
    assert att["observed_status"] == "provider_error" and att["error_class"] == "APITimeoutError"
    assert att["http_status"] is None
    assert one(conn, "SELECT billing_status FROM charges")["billing_status"] == "unknown"


def test_install_twice_wraps_once(harness: Harness) -> None:
    from openai.resources.chat.completions import Completions

    before = Completions.create
    assert openai_chat.install(harness.store.emit)["already"] is True
    assert Completions.create is before
    client = client_for(chat_json())
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.conn()
    assert one(conn, "SELECT COUNT(*) FROM attempts")[0] == 1
    assert one(conn, "SELECT COUNT(*) FROM source_observations")[0] == 1


@pytest.mark.parametrize("where", ["before", "after"])
def test_hook_failure_preserves_call_and_result(harness: Harness, where: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(chat_json(content="still here")).encode())

    client = make_client(handler)
    openai_chat._FAULT = where
    try:
        r = client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    finally:
        openai_chat._FAULT = None
    assert r.choices[0].message.content == "still here"
    assert calls == 1  # exactly one invocation, never re-issued
    conn = harness.conn()
    kinds = [r["kind"] for r in rows(conn, "SELECT kind FROM health_events")]
    assert f"hook_failure_{where}" in kinds
    if where == "before":
        assert one(conn, "SELECT COUNT(*) FROM attempts")[0] == 0  # nothing to attach to; counted in health
    else:
        att = one(conn, "SELECT observed_status FROM attempts")
        assert att["observed_status"] == "in_progress"  # terminal hook failed: start remains, visible


def test_hook_failure_after_provider_exception_reraises(harness: Harness) -> None:
    client = client_for({"error": {"message": "x"}}, status=500)
    with pytest.raises(openai.InternalServerError):
        client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)


def test_with_raw_response_covered_when_patched_first(harness: Harness) -> None:
    client = client_for(chat_json(prompt_tokens=5, completion_tokens=5, object_id="chatcmpl-raw"), request_id="req-raw")
    raw = client.chat.completions.with_raw_response.create(model="gpt-4.1-mini", messages=MSG)
    assert raw.parse().id == "chatcmpl-raw"
    conn = harness.account()
    assert one(conn, "SELECT observed_status FROM attempts")["observed_status"] == "completed"
    assert one(conn, "SELECT amount_nanos FROM cost_estimates")["amount_nanos"] == 2_000 + 8_000


def test_with_raw_response_cached_before_patch_is_not_covered(harness: Harness) -> None:
    openai_chat.uninstall()
    client = client_for(chat_json(prompt_tokens=5, completion_tokens=5))
    _ = client.chat.completions.with_raw_response  # cache the wrapper around the ORIGINAL bound method
    openai_chat.install(harness.store.emit)
    client.chat.completions.with_raw_response.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.conn()
    assert one(conn, "SELECT COUNT(*) FROM attempts")[0] == 0  # documented gap


def test_stream_is_returned_untouched_and_recorded_unsupported(harness: Harness) -> None:
    sse = b'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","created":1,"model":"gpt-4.1-mini",' \
          b'"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    client = make_client(handler)
    chunks = list(client.chat.completions.create(model="gpt-4.1-mini", messages=MSG, stream=True))
    assert chunks[0].choices[0].delta.content == "hi"
    conn = harness.account()
    assert one(conn, "SELECT observed_status FROM attempts")["observed_status"] == "unsupported_stream"
    assert one(conn, "SELECT supported FROM operations")["supported"] == 0
    assert one(conn, "SELECT estimation_status FROM cost_estimates")["estimation_status"] == "no_usage"
    assert "unsupported_surface" in [r["kind"] for r in rows(conn, "SELECT kind FROM health_events")]
