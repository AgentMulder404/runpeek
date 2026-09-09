from __future__ import annotations

import re

import httpx
import openai
import pytest

import nemulai
from conftest import Harness, chat_json, client_for, make_client
from nemulai import summary as summ
from nemulai.perspective import DEFAULT

MSG = [{"role": "user", "content": "hi"}]


def test_buckets_reconcile(harness: Harness) -> None:
    ok = client_for(chat_json(prompt_tokens=1000, completion_tokens=500))          # priced 0.0012
    unknown = client_for(chat_json(model="mystery", prompt_tokens=10, completion_tokens=10))
    nousage = client_for(chat_json(prompt_tokens=None, completion_tokens=None))
    err = client_for({"error": {"message": "x"}}, status=500)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("t", request=request)

    slow = make_client(timeout)
    with nemulai.job(customer="acme"):
        ok.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
        ok.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
        unknown.chat.completions.create(model="mystery", messages=MSG)
        with pytest.raises(openai.APIError):
            err.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    with nemulai.job(customer="globex"):
        ok.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
        nousage.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    with nemulai.job(job="batch"):
        ok.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    ok.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    with pytest.raises(openai.APIError):
        slow.chat.completions.create(model="gpt-4.1-mini", messages=MSG)

    conn = harness.account()
    s = summ.build(conn, harness.run_id, DEFAULT)
    # attempts = operations = charges, and the status buckets partition them
    assert s.attempts == s.operations == s.charges == 9
    assert sum(s.status_counts.values()) == 9
    assert s.status_counts == {"completed": 7, "provider_error": 2}
    # estimation buckets partition charges
    assert s.est_counts == {"priced": 5, "unpriced": 1, "no_usage": 3}
    assert s.known_cost_nanos == 5 * 1_200_000
    # usage coverage over completed attempts
    assert s.usage_exact + s.usage_missing == s.status_counts["completed"]
    assert s.usage_exact == 6 and s.usage_missing == 1
    # billing buckets partition charges
    assert s.billing_counts == {"expected": 6, "unknown": 3}
    # attribution by cost partitions known cost
    assert sum(s.attribution_cost.values()) == s.known_cost_nanos
    assert s.attribution_cost == {"attributed": 3 * 1_200_000, "job_only": 1_200_000, "unattributed": 1_200_000}
    # customer table: ops sum to operations, cost sums to known cost, shares to 100 %
    assert sum(c["ops"] for c in s.by_customer) == 9
    assert sum(c["cost"] for c in s.by_customer) == s.known_cost_nanos
    assert abs(sum(c["share"] for c in s.by_customer) - 1.0) < 1e-9
    names = {c["customer"] for c in s.by_customer}
    assert names == {"acme", "globex", "(job only)", "(unattributed)"}
    assert sum(m["ops"] for m in s.by_model) == 9
    text = summ.render(s)
    assert "KNOWN ESTIMATED COST   $0.006" in text and "not actual spend" in text
    assert re.search(r"unpriced\s+1 charges", text) and '"mystery" (1)' in text
    assert re.search(r"no_usage\s+3 charges", text)
    assert "(job only)" in text and "(unattributed)" in text
    assert "capture: not measurable" in text


def test_zero_observations_message(harness: Harness) -> None:
    conn = harness.account()
    text = summ.render(summ.build(conn, harness.run_id, DEFAULT))
    assert "No AI operations were observed" in text
    assert "does not mean no AI spend" in text
