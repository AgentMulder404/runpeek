"""Documented-telemetry receiver: sanitized real OTLP payloads from Claude Code 2.1.269 and Codex CLI 0.154.0,
allowlisting, idempotency, overlap with transcript adapters, auth/size/type rejection over real HTTP, and
concurrent sessions on different tasks."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from codex_fixtures import CWD, Rollout
from runpeek import telemetry
from runpeek.agents import codex, work
from runpeek.agents.ingest import Ingestor, IngestStats
from runpeek.store import apply_schema, open_connection

FIXTURES = Path(__file__).parent / "fixtures" / "otel"
SENSITIVE = ("user@example.invalid", "user_TESTACCOUNT", "00000000-0000-4000-8000-000000000001",
             "00000000-0000-4000-8000-000000000002", "test-host")


def _bodies(name: str) -> list[dict]:
    return [json.loads(line)["body"] for line in (FIXTURES / name).read_text().splitlines()]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(tmp_path / "cc"))
    codex._head_cache.clear()
    return tmp_path


# ------------------------------------------------------------------ parsing real payloads


def test_claude_code_real_payload_yields_api_requests_only_with_allowlisted_fields() -> None:
    events = [e for body in _bodies("claude-code-2.1.269.jsonl") for e in telemetry.parse_logs(body)]
    assert len(events) == 2  # one auxiliary (session title) call, one main call; plugin/mcp events ignored
    aux, main = events
    assert aux["source"] == "claude-code" and aux["provider"] == "anthropic"
    assert aux["model"] == "claude-haiku-4-5-20251001" and aux["query_source"] == "generate_session_title"
    assert (aux["input_tokens"], aux["output_tokens"], aux["cache_read_tokens"], aux["cache_write_5m_tokens"]) == (
        899, 8, 0, 0)
    assert aux["source_cost_nanos"] == 939_000  # cost_usd_micros 939
    assert main["request_id"].startswith("req_") and main["session_id"] == aux["session_id"]
    assert (main["input_tokens"], main["cache_read_tokens"], main["cache_write_5m_tokens"], main["output_tokens"]) == (
        10, 13607, 8295, 33)
    dump = json.dumps(events)
    for s in SENSITIVE:
        assert s not in dump
    assert "prompt" not in dump and "response" not in dump


def test_codex_real_payload_yields_response_completed_only() -> None:
    events = [e for body in _bodies("codex-0.154.0.jsonl") for e in telemetry.parse_logs(body)]
    assert len(events) == 2 and all(e["source"] == "codex" and e["provider"] == "openai" for e in events)
    warm, real = events
    assert (warm["input_tokens"], warm["cache_read_tokens"], warm["output_tokens"]) == (13079, 0, 0)
    # input_token_count includes cached tokens: normalised to uncached input + cache read
    assert (real["input_tokens"], real["cache_read_tokens"], real["output_tokens"], real["reasoning_tokens"]) == (
        18215 - 12928, 12928, 5, 0)
    assert real["model"] == "gpt-6-astra" and real["request_id"] is None
    assert real["session_id"] == warm["session_id"] and len(real["session_id"]) == 36
    dump = json.dumps(events)
    for s in SENSITIVE:
        assert s not in dump


# ------------------------------------------------------------------ storage


def test_store_is_idempotent_and_prices_with_source_cost(conn: sqlite3.Connection) -> None:
    events = [e for body in _bodies("claude-code-2.1.269.jsonl") for e in telemetry.parse_logs(body)]
    first = telemetry.store_events(conn, events)
    again = telemetry.store_events(conn, events)
    assert first["inserted"] == 2 and again == {"inserted": 0, "merged": 0, "duplicate": 2, "quarantined": 0}
    rows = conn.execute("SELECT * FROM agent_usage ORDER BY at").fetchall()
    assert len(rows) == 2 and all(r["provenance"] == "telemetry" and r["provider"] == "anthropic" for r in rows)
    # rate-card estimate agrees with the agent's own figure for the auxiliary call (899 in, 8 out at haiku prices)
    assert rows[0]["api_equiv_nanos"] == 899 * 1_000 + 8 * 5_000 == rows[0]["source_cost_nanos"]
    sess = conn.execute("SELECT * FROM agent_sessions").fetchone()
    assert sess["source"] == "claude-code" and sess["transcript_path"].startswith("telemetry://")
    assert sess["telemetry_last_at"] is not None and sess["source_version"] == "2.1.269"
    dump = "\n".join(json.dumps(dict(r)) for t in ("agent_sessions", "agent_usage")
                     for r in conn.execute(f"SELECT * FROM {t}"))
    for s in SENSITIVE:
        assert s not in dump


def test_claude_transcript_and_telemetry_describe_one_request(conn: sqlite3.Connection, homes: Path) -> None:
    from agent_fixtures import Transcript, usage_block

    events = [e for body in _bodies("claude-code-2.1.269.jsonl") for e in telemetry.parse_logs(body)]
    main = events[1]
    # a transcript for the same session whose assistant entry carries the same requestId
    t = Transcript(homes / "cc", CWD, session_id=main["session_id"],
                   start=datetime(2026, 9, 11, 21, 47, tzinfo=timezone.utc))
    t.user_prompt()
    entry_before = t.path.read_text()
    t.assistant([{"type": "text", "text": "SECRET_ASSISTANT_TEXT"}],
                usage=usage_block(inp=10, out=33, cw5=8295, cw1=0, cr=13607), model="claude-haiku-4-5-20251001")
    lines = t.path.read_text().splitlines()
    last = json.loads(lines[-1])
    last["requestId"] = main["request_id"]
    t.path.write_text(entry_before + json.dumps(last) + "\n")
    from runpeek.agents import claude_code

    # order 1: telemetry first, then transcript
    telemetry.store_events(conn, [main])
    ing = Ingestor(conn)
    st = IngestStats()
    for tf in claude_code.discover(CWD):
        ing.ingest(tf, st)
    assert st.merged_with_telemetry == 1 and st.usage == 0
    rows = conn.execute("SELECT provenance, request_id, ordinal FROM agent_usage").fetchall()
    assert len(rows) == 1 and rows[0]["provenance"] == "transcript+telemetry"
    # order 2: transcript first, then telemetry (fresh store)
    c2 = open_connection(homes / "n2.db")
    apply_schema(c2)
    ing2 = Ingestor(c2)
    for tf in claude_code.discover(CWD):
        ing2.ingest(tf)
    res = telemetry.store_events(c2, [main])
    assert res["merged"] == 1 and res["inserted"] == 0
    rows = c2.execute("SELECT provenance, source_cost_nanos, usage_id FROM agent_usage").fetchall()
    assert len(rows) == 1 and rows[0]["provenance"] == "transcript+telemetry"
    assert rows[0]["source_cost_nanos"] == main["source_cost_nanos"] and rows[0]["usage_id"].startswith("msg_")
    # the session row created by the transcript keeps its real path; the telemetry-only row would have a marker
    assert not c2.execute("SELECT transcript_path FROM agent_sessions").fetchone()[0].startswith("telemetry://")


def test_codex_transcript_rows_are_quarantined_when_telemetry_is_authoritative(conn: sqlite3.Connection,
                                                                               homes: Path) -> None:
    events = [e for body in _bodies("codex-0.154.0.jsonl") for e in telemetry.parse_logs(body)]
    sid = events[0]["session_id"]
    r = Rollout(homes / "cx", model="gpt-6-astra", thread_id=sid,
                start=datetime(2026, 9, 11, 21, 48, tzinfo=timezone.utc))
    r.user_turn()
    r.usage(18215, 12928, 5)
    r.complete()
    # telemetry first
    telemetry.store_events(conn, events)
    ing = Ingestor(conn)
    st = IngestStats()
    for tf in codex.discover(CWD):
        ing.ingest(tf, st)
    assert st.usage == 1 and st.quarantined == 1
    q = conn.execute("SELECT api_equiv_status, quarantine_reason FROM agent_usage"
                     " WHERE provenance = 'provider_reported'").fetchall()
    assert [tuple(x) for x in q] == [("quarantined", "telemetry_authoritative")]
    assert conn.execute("SELECT COUNT(*) FROM agent_usage WHERE provenance = 'telemetry'").fetchone()[0] == 2
    # the reverse order: transcript first, telemetry later quarantines retroactively
    c2 = open_connection(homes / "n2.db")
    apply_schema(c2)
    ing2 = Ingestor(c2)
    for tf in codex.discover(CWD):
        ing2.ingest(tf)
    res = telemetry.store_events(c2, events)
    assert res["inserted"] == 2 and res["quarantined"] == 1
    # the work report counts telemetry rows only, and says why
    wid = work.create(c2, "Codex task", "task")
    work.assign(c2, wid, [sid])
    rep = work.build_report(c2, wid)
    assert rep.total.calls == 3 and rep.total.quarantined_calls == 1 and rep.total.priced_calls == 2
    text = work.render_report(rep)
    assert "1 transcript calls quarantined" in text and "telemetry is authoritative" in text


# ------------------------------------------------------------------ the receiver over HTTP


def _post(port: int, path: str, body: bytes, *, token: str | None, ctype: str = "application/json") -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                 headers={"Content-Type": ctype, **({"Authorization": f"Bearer {token}"} if token
                                                                     else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(r.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def test_receiver_auth_size_type_and_signals(tmp_path: Path) -> None:
    db = tmp_path / "n.db"
    c = open_connection(db)
    apply_schema(c)
    token = telemetry.ensure_token(c)
    c.close()

    def factory() -> sqlite3.Connection:
        cc = open_connection(db)
        apply_schema(cc)
        return cc

    rx = telemetry.Receiver(factory, token, port=0)
    th = threading.Thread(target=rx.serve_forever, daemon=True)
    th.start()
    try:
        body = json.dumps(_bodies("claude-code-2.1.269.jsonl")[1]).encode()
        assert _post(rx.port, "/v1/logs", body, token=None) == 401
        assert _post(rx.port, "/v1/logs", body, token="wrong") == 401
        assert _post(rx.port, "/v1/logs", body, token=token, ctype="application/x-protobuf") == 415
        assert _post(rx.port, "/v1/logs", b"{" * 10, token=token) == 400
        assert _post(rx.port, "/v1/metrics", b'{"resourceMetrics":[]}', token=token) == 200
        assert _post(rx.port, "/v1/traces", b"\x00\x01", token=token, ctype="application/x-protobuf") == 200
        assert _post(rx.port, "/v1/logs", body, token=token) == 200
        assert _post(rx.port, "/v1/logs", body, token=token) == 200  # duplicate delivery
        big = b"{" + b" " * (telemetry.MAX_BODY + 10) + b"}"
        assert _post(rx.port, "/v1/logs", big, token=token) == 413
        with urllib.request.urlopen(f"http://127.0.0.1:{rx.port}/healthz", timeout=5) as r:
            assert r.status == 200
    finally:
        rx.shutdown()
    c = open_connection(db)
    assert c.execute("SELECT COUNT(*) FROM agent_usage").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM agent_usage WHERE provenance = 'telemetry'").fetchone()[0] == 1
    assert rx.stats["rejected_auth"] == 2 and rx.stats["rejected_type"] == 1 and rx.stats["malformed"] == 1
    assert rx.stats["rejected_size"] == 1 and rx.stats["ignored_signals"] == 2 and rx.stats["duplicate"] == 1
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%metric%'").fetchone()[0] == 0


# ------------------------------------------------------------------ concurrency and attribution


def test_two_agents_on_two_tasks_concurrently(conn: sqlite3.Connection) -> None:
    claude = [e for body in _bodies("claude-code-2.1.269.jsonl") for e in telemetry.parse_logs(body)]
    cx = [e for body in _bodies("codex-0.154.0.jsonl") for e in telemetry.parse_logs(body)]
    # interleave arrival order
    telemetry.store_events(conn, [claude[0]])
    telemetry.store_events(conn, [cx[0]])
    a = work.create(conn, "Build login", "feature")
    b = work.create(conn, "Fix export bug", "bugfix")
    work.assign(conn, a, [claude[0]["session_id"]])
    work.assign(conn, b, [cx[0]["session_id"]])
    telemetry.store_events(conn, [claude[1]])
    telemetry.store_events(conn, [cx[1]])
    ra, rb = work.build_report(conn, a), work.build_report(conn, b)
    assert {ln.source for ln in ra.sessions} == {"claude-code"} and {ln.source for ln in rb.sessions} == {"codex"}
    assert ra.total.calls == 2 and rb.total.calls == 2
    assert ra.total.cost_nanos == 939_000 + 18_126_000  # agent-reported cost_usd_micros
    assert ra.total.source_reported_calls == 2 and rb.total.source_reported_calls == 0
    text = work.render_report(ra)
    assert "ACCOUNTED COST FOR THIS TASK" in text and "provisional usage" in text
    assert "agent-reported cost" in text
    # no cross-task leakage on re-delivery
    telemetry.store_events(conn, claude + cx)
    assert work.build_report(conn, a).total.cost_nanos == ra.total.cost_nanos
    assert work.build_report(conn, b).total.calls == 2


def test_unknown_model_and_missing_usage_from_telemetry(conn: sqlite3.Connection) -> None:
    body = _bodies("claude-code-2.1.269.jsonl")[1]
    ev = telemetry.parse_logs(body)[0]
    ev = dict(ev, model="claude-unknown-9", usage_id="otel:req_x", request_id="req_x")
    missing = dict(ev, model="claude-haiku-4-5-20251001", usage_id="otel:req_y", request_id="req_y",
                   input_tokens=None, output_tokens=None, cache_read_tokens=None, source_cost_nanos=None)
    telemetry.store_events(conn, [ev, missing])
    rows = {r["usage_id"]: r for r in conn.execute("SELECT * FROM agent_usage")}
    assert rows["otel:req_x"]["api_equiv_status"] == "unpriced" and rows["otel:req_x"]["api_equiv_nanos"] is None
    assert rows["otel:req_y"]["api_equiv_status"] == "priced" and rows["otel:req_y"]["api_equiv_nanos"] == 0
    wid = work.create(conn, "gaps", "task")
    work.assign(conn, wid, [ev["session_id"]])
    rep = work.build_report(conn, wid)
    # the unknown model's agent-reported cost is still used (it is the agent's own figure), the unpriced
    # count reflects the rate-card view for the model-less row
    assert rep.total.calls == 2 and rep.total.priced_calls == 2
