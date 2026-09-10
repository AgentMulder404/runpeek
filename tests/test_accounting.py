from __future__ import annotations

import json
from pathlib import Path

import runpeek
from conftest import Harness, chat_json, client_for, one, rows
from runpeek import accounting
from runpeek.accounting import Usage, ingest_observation
from runpeek.perspective import DEFAULT, pinned
from runpeek.rates import RateCardSet, load_card

MSG = [{"role": "user", "content": "hi"}]


def test_two_sources_same_request_one_charge(harness: Harness) -> None:
    client = client_for(chat_json(prompt_tokens=1000, completion_tokens=500, object_id="chatcmpl-a1"),
                        request_id="req-a1")
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    # a proxy export reports the same request id with usage inside tolerance
    conn.execute("BEGIN")
    att = ingest_observation(
        conn, run_id="run_proxy", source="proxy", provider="openai",
        identifiers=[("http_request_id", "openai.x-request-id", "req-a1")],
        usage=Usage(1005, 500), model_served="gpt-4.1-mini",
    )
    conn.execute("COMMIT")
    accounting.run(conn, DEFAULT, RateCardSet.builtin())
    assert att == one(conn, "SELECT attempt_id FROM attempts")["attempt_id"]
    assert one(conn, "SELECT COUNT(*) FROM attempts")[0] == 1
    assert one(conn, "SELECT COUNT(*) FROM source_observations")[0] == 2
    assert one(conn, "SELECT COUNT(*) FROM charges")[0] == 1
    assert one(conn, "SELECT class FROM correlations")["class"] == "exact"
    sel = one(conn, "SELECT source, input_tokens FROM source_observations WHERE selected = 1")
    assert sel["source"] == "harness.openai" and sel["input_tokens"] == 1000  # precedence, not averaging
    assert one(conn, "SELECT COUNT(*) FROM cost_estimates WHERE current = 1")[0] == 1


def test_conflicting_usage_is_flagged_and_kept(harness: Harness) -> None:
    client = client_for(chat_json(prompt_tokens=1000, completion_tokens=500), request_id="req-c")
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    conn.execute("BEGIN")
    ingest_observation(conn, run_id="r", source="proxy", provider="openai",
                       identifiers=[("http_request_id", "openai.x-request-id", "req-c")],
                       usage=Usage(1200, 500), model_served="gpt-4.1-mini")
    conn.execute("COMMIT")
    accounting.run(conn, DEFAULT, RateCardSet.builtin())
    assert one(conn, "SELECT class FROM correlations")["class"] == "conflicting"
    assert one(conn, "SELECT COUNT(*) FROM source_observations")[0] == 2
    assert one(conn, "SELECT amount_nanos FROM cost_estimates WHERE current = 1")["amount_nanos"] == 1_200_000


def test_same_operation_different_requests_stay_distinct(harness: Harness) -> None:
    client = client_for(chat_json(prompt_tokens=100, completion_tokens=100, object_id="chatcmpl-x"),
                        request_id="req-x")
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    op_id = one(conn, "SELECT operation_id FROM operations")["operation_id"]
    # A proxy saw two HTTP requests for this operation (an internal retry the SDK hid).
    conn.execute("BEGIN")
    a1 = ingest_observation(conn, run_id="r", source="proxy", provider="openai",
                            identifiers=[("harness_operation_id", "runpeek", op_id),
                                         ("http_request_id", "openai.x-request-id", "req-x-first")],
                            usage=Usage(None, None), model_served=None, http_status=500)
    a2 = ingest_observation(conn, run_id="r", source="proxy", provider="openai",
                            identifiers=[("harness_operation_id", "runpeek", op_id),
                                         ("http_request_id", "openai.x-request-id", "req-x")],
                            usage=Usage(100, 100), model_served="gpt-4.1-mini")
    conn.execute("COMMIT")
    accounting.run(conn, DEFAULT, RateCardSet.builtin())
    assert a1 != a2
    assert one(conn, "SELECT operation_id FROM attempts WHERE attempt_id = ?", a1)["operation_id"] == op_id
    assert one(conn, "SELECT COUNT(*) FROM attempts")[0] == 2  # grouped under one operation, not merged
    assert one(conn, "SELECT COUNT(*) FROM charges")[0] == 2
    assert one(conn, "SELECT COUNT(*) FROM source_observations WHERE attempt_id = ?", a2)[0] == 2


def test_estimates_idempotent_and_pinned_reprice_is_separate(harness: Harness, tmp_path: Path) -> None:
    client = client_for(chat_json(prompt_tokens=1000, completion_tokens=500))
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    r1 = accounting.run(conn, DEFAULT, RateCardSet.builtin())
    r2 = accounting.run(conn, DEFAULT, RateCardSet.builtin())
    assert r1["estimates_created"] == 0 and r2["estimates_created"] == 0
    assert one(conn, "SELECT COUNT(*) FROM cost_estimates")[0] == 1

    cheaper = tmp_path / "cheaper.json"
    cheaper.write_text(json.dumps({
        "rate_card_id": "openai-list@2099-01-01", "provider": "openai", "source": "list",
        "effective_from": "2099-01-01", "models": {"gpt-4.1-mini": {"input": "0.40", "cached_input": "0.10",
                                                                    "output": "1.20"}},
    }))
    cards = RateCardSet.builtin([load_card(cheaper)])
    p = pinned("openai-list@2099-01-01")
    accounting.run(conn, p, cards)
    accounting.run(conn, p, cards)  # idempotent under the pinned perspective too
    ests = rows(conn, "SELECT perspective_id, amount_nanos, rate_resolution, current FROM cost_estimates"
                      " ORDER BY perspective_id")
    assert [(e["perspective_id"], e["amount_nanos"], e["rate_resolution"], e["current"]) for e in ests] == [
        ("default", 1_200_000, "effective_at_execution", 1),
        ("pinned:openai-list@2099-01-01", 1_000_000, "pinned", 1),
    ]
    # exactly one current estimate per (charge, perspective)
    dup = rows(conn, "SELECT charge_id, perspective_id, COUNT(*) n FROM cost_estimates WHERE current = 1"
                     " GROUP BY charge_id, perspective_id HAVING n > 1")
    assert dup == []


def test_new_selection_supersedes_but_keeps_history(harness: Harness) -> None:
    # framework-only observation first (aggregate), then the harness view arrives via proxy with an id
    conn = harness.account()
    conn.execute("BEGIN")
    att = ingest_observation(conn, run_id="r", source="framework", provider="openai",
                             identifiers=[("proxy_request_id", "p", "z1")], usage=Usage(200, 100),
                             model_served="gpt-4.1-mini")
    conn.execute("COMMIT")
    accounting.run(conn, DEFAULT, RateCardSet.builtin())
    first = one(conn, "SELECT estimate_key, selected_observation_id FROM cost_estimates WHERE current = 1")
    conn.execute("BEGIN")
    ingest_observation(conn, run_id="r", source="proxy", provider="openai",
                       identifiers=[("proxy_request_id", "p", "z1")], usage=Usage(201, 100),
                       model_served="gpt-4.1-mini")
    conn.execute("COMMIT")
    accounting.run(conn, DEFAULT, RateCardSet.builtin())
    assert one(conn, "SELECT COUNT(*) FROM attempts")[0] == 1 and att
    cur = one(conn, "SELECT estimate_key, selected_observation_id, revision FROM cost_estimates WHERE current = 1")
    assert cur["selected_observation_id"] != first["selected_observation_id"] and cur["revision"] == 2
    old = one(conn, "SELECT current, superseded_by FROM cost_estimates WHERE estimate_key = ?", first["estimate_key"])
    assert old["current"] == 0 and old["superseded_by"] == cur["estimate_key"]


def test_unattributed_call_is_a_first_class_row(harness: Harness) -> None:
    client = client_for(chat_json(prompt_tokens=10, completion_tokens=10))
    client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    with runpeek.job(job="nameless"):
        client.chat.completions.create(model="gpt-4.1-mini", messages=MSG)
    conn = harness.account()
    states = sorted(r["attribution_state"] for r in rows(conn, "SELECT attribution_state FROM operations"))
    assert states == ["job_only", "unattributed"]
