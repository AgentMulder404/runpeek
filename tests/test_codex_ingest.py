"""Codex adapter: cumulative-usage handling, stale repeats, subagents, tool
errors, unknown versions, unpriced models, incremental and duplicate imports —
against synthetic rollouts and sanitised real records."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from codex_fixtures import CWD, REAL_PARENT, REAL_STALE, REAL_SUBAGENT, SENTINELS, Rollout, install_real_fixture
from runpeek.agents import codex
from runpeek.agents.ingest import Ingestor, IngestStats
from runpeek.store import apply_schema, open_connection


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "codex-home"
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(h))
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(tmp_path / "claude-home"))
    codex._head_cache.clear()
    return h


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


def _ingest_all(conn: sqlite3.Connection, project: str | None = CWD, **kw: object) -> IngestStats:
    ing = Ingestor(conn, **kw)  # type: ignore[arg-type]
    stats = IngestStats()
    for tf in codex.discover(project, all_projects=project is None):
        ing.ingest(tf, stats)
    return stats


def _count(conn: sqlite3.Connection, table: str, where: str = "", *params: object) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])


def _usage_totals(conn: sqlite3.Connection, session_id: str) -> tuple[int, int, int, int]:
    r = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(cache_read_tokens),0) c,"
        " COALESCE(SUM(output_tokens),0) o FROM agent_usage WHERE session_id = ?", (session_id,)).fetchone()
    return int(r["n"]), int(r["i"]), int(r["c"]), int(r["o"])


# ------------------------------------------------------------------ discovery


def test_discovery_uses_file_uuid_and_session_meta_cwd(home: Path) -> None:
    a = Rollout(home)
    b = Rollout(home, cwd="/work/other")
    sub = Rollout(home, parent=a, history_start_ordinal=3)
    found = codex.discover(CWD)
    ids = {tf.session_id for tf in found}
    assert a.thread_id in ids and sub.thread_id in ids and b.thread_id not in ids
    s = next(tf for tf in found if tf.session_id == sub.thread_id)
    assert s.is_subagent and s.parent_session_id == a.thread_id and s.source == "codex"
    assert len(codex.discover(None, all_projects=True)) == 3
    assert codex.version_supported("0.153.1") and codex.version_supported("0.130.0-alpha.5")
    assert not codex.version_supported("0.200.0") and not codex.version_supported("1.0.0")


# ------------------------------------------------------------------ usage semantics


def test_usage_is_the_delta_of_cumulative_totals_not_last_usage(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home)
    r.user_turn()
    r.usage(1000, 600, 50, 10)
    r.usage(1500, 1200, 80, 20)
    r.abort()
    r.user_turn()
    r.usage(0, 0, 0, repeat_stale=True)  # the pattern after turn_aborted: last repeated, total unchanged
    r.usage(2000, 1900, 30)
    r.complete()
    s = _ingest_all(conn)
    assert s.usage == 3  # the stale repeat is not a request
    rows = conn.execute("SELECT * FROM agent_usage ORDER BY at").fetchall()
    # normalised: input = uncached input; cache_read = cached; output includes reasoning (kept separately)
    assert [(u["input_tokens"], u["cache_read_tokens"], u["output_tokens"], u["reasoning_tokens"]) for u in rows] == [
        (400, 600, 50, 10), (300, 1200, 80, 20), (100, 1900, 30, 0)]
    assert all(u["usage_kind"] == "per_request" and u["provider"] == "openai" for u in rows)
    assert all(u["usage_id"] == f"{r.thread_id}:{u['ordinal']}" for u in rows)
    sess = conn.execute("SELECT * FROM agent_sessions").fetchone()
    assert json.loads(sess["usage_consistency"])["stale_usage_repeats"] == 1
    assert sess["source"] == "codex" and sess["provider"] == "openai" and sess["git_branch"] == "main"
    # turns: aborted turn has the source's duration; completed turn's duration is derived from started_at
    turns = conn.execute("SELECT duration_ms FROM agent_turns ORDER BY started_at").fetchall()
    assert turns[0]["duration_ms"] == 1400 and turns[1]["duration_ms"] is not None and turns[1]["duration_ms"] > 0


def test_summing_last_token_usage_would_overcount(home: Path, conn: sqlite3.Connection) -> None:
    """Documents why the adapter does not sum last_token_usage."""
    r = Rollout(home)
    r.user_turn()
    r.usage(1000, 0, 10)
    r.usage(0, 0, 0, repeat_stale=True)
    r.usage(0, 0, 0, repeat_stale=True)
    naive = 0
    for line in r.path.read_text().splitlines():
        d = json.loads(line)
        if d["type"] == "event_msg" and d["payload"].get("type") == "token_count":
            naive += d["payload"]["info"]["last_token_usage"]["input_tokens"]
    assert naive == 3000
    _ingest_all(conn)
    assert _usage_totals(conn, r.thread_id) == (1, 1000, 0, 10)


def test_token_usage_record_only_supplies_the_response_id(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home)
    r.user_turn()
    r.usage(100, 0, 5, response_id="resp_abc")
    r.usage(100, 0, 5, response_id="resp_def")
    _ingest_all(conn)
    rows = conn.execute("SELECT request_id, input_tokens FROM agent_usage ORDER BY at").fetchall()
    assert [(x["request_id"], x["input_tokens"]) for x in rows] == [("resp_abc", 100), ("resp_def", 100)]


def test_usage_before_any_model_context_stays_unpriced_and_is_counted(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home)
    r.usage(100, 0, 5)  # no turn_context yet → no model
    r.user_turn()
    r.usage(100, 0, 5)
    _ingest_all(conn)
    rows = conn.execute("SELECT model, api_equiv_status FROM agent_usage ORDER BY at").fetchall()
    assert rows[0]["model"] is None and rows[0]["api_equiv_status"] == "unpriced"
    assert rows[1]["model"] == "gpt-5.5"
    assert json.loads(conn.execute("SELECT usage_consistency FROM agent_sessions").fetchone()[0])[
        "usage_without_model"] == 1


def test_cached_token_pricing_openai(home: Path, conn: sqlite3.Connection) -> None:
    from datetime import datetime, timezone

    r = Rollout(home, model="gpt-4.1-mini", start=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc))
    r.user_turn()
    r.usage(10_000, 4_000, 1_000)  # input includes the cached part (OpenAI semantics)
    _ingest_all(conn)
    u = conn.execute("SELECT * FROM agent_usage").fetchone()
    # gpt-4.1-mini list: input 0.40, cached 0.10, output 1.60 per M
    assert (u["input_tokens"], u["cache_read_tokens"], u["output_tokens"]) == (6_000, 4_000, 1_000)
    assert u["api_equiv_nanos"] == 6_000 * 400 + 4_000 * 100 + 1_000 * 1_600
    assert u["api_equiv_status"] == "priced" and u["rate_card_id"] == "openai-list@2026-09-10"


def test_unknown_price_for_the_effective_card_is_unpriced_not_zero(home: Path, conn: sqlite3.Connection) -> None:
    from datetime import datetime, timezone

    # gpt-5.5 was verified on 2026-09-10; usage in July resolves to the 2025-08-01 card, which lacks it
    r = Rollout(home, model="gpt-5.5", start=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc))
    r.user_turn()
    r.usage(1000, 0, 10)
    _ingest_all(conn)
    u = conn.execute("SELECT * FROM agent_usage").fetchone()
    assert u["api_equiv_status"] == "unpriced" and u["api_equiv_nanos"] is None
    assert u["rate_card_id"] == "openai-list@2025-08-01" and u["rate_resolution"] == "effective_at_execution"


# ------------------------------------------------------------------ actions


def test_tool_calls_targets_and_errors(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home)
    r.user_turn()
    r.exec("pytest -q", exit_code=1)
    r.exec("pytest -q", exit_code=1, as_function=True)
    r.exec("ls", exit_code=None)
    r.patch("src/app.py")
    r.mcp_item("apify", "search_actors", failed=True)
    r.complete()
    _ingest_all(conn)
    rows = conn.execute("SELECT tool_name, action_kind, target, is_error FROM agent_actions"
                        " ORDER BY sequence").fetchall()
    assert [tuple(x) for x in rows] == [
        ("exec", "bash", "pytest", 1), ("exec_command", "bash", "pytest", 1), ("exec", "bash", "ls", None),
        ("apply_patch", "edit", "src/app.py", None), ("mcp:apify/search_actors", "mcp", None, 1)]


def test_privacy_nothing_content_like_is_stored(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home)
    r.user_turn()
    r.exec("curl -H 'Authorization: SECRET_COMMAND_PAYLOAD' https://x", exit_code=1)
    r.patch("secret_dir/notes.md")
    r.mcp_item("apify", "call_actor")
    r.usage(100, 0, 5)
    r.complete()
    _ingest_all(conn)
    dump = "\n".join(json.dumps(dict(x)) for tbl in ("agent_sessions", "agent_turns", "agent_actions", "agent_usage",
                                                      "watch_checkpoints")
                     for x in conn.execute(f"SELECT * FROM {tbl}"))
    for s in SENTINELS:
        assert s not in dump, s
    assert "Authorization" not in dump and '"target": "curl"' in dump and '"target": "secret_dir/notes.md"' in dump


# ------------------------------------------------------------------ subagents


def test_subagent_usage_is_independent_and_copied_prefix_is_skipped(home: Path, conn: sqlite3.Connection) -> None:
    parent = Rollout(home)
    parent.user_turn()
    parent.usage(1000, 0, 10)
    child = Rollout(home, parent=parent, history_start_ordinal=6)
    # copied history (ordinals below the start) — a token_count here must not be counted
    child.append("turn_context", {"turn_id": "t-copied", "cwd": CWD, "model": "gpt-5.5"}, ordinal=3)
    child.append("event_msg", {"type": "token_count", "info": {"total_token_usage": dict(parent.total),
                                                                "last_token_usage": dict(parent.last)}}, ordinal=4)
    child.ordinal = 6
    child.user_turn()
    child.usage(300, 100, 7)
    child.complete()
    parent.spawn_agent(child.thread_id)
    parent.usage(500, 0, 5)
    parent.complete()
    s = _ingest_all(conn)
    assert s.usage == 3
    assert _usage_totals(conn, parent.thread_id) == (2, 1500, 0, 15)
    assert _usage_totals(conn, child.thread_id) == (1, 200, 100, 7)
    sub = conn.execute("SELECT * FROM agent_sessions WHERE session_id = ?", (child.thread_id,)).fetchone()
    assert sub["is_subagent"] == 1 and sub["parent_session_id"] == parent.thread_id
    assert json.loads(sub["usage_consistency"])["usage_in_copied_prefix"] == 1


# ------------------------------------------------------------------ imports


def test_repeated_and_incremental_imports_do_not_double_count(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home)
    r.user_turn()
    r.exec("make")
    r.usage(1000, 500, 10)
    _ingest_all(conn)
    before = (_count(conn, "agent_usage"), _count(conn, "agent_actions"), _usage_totals(conn, r.thread_id))
    _ingest_all(conn)  # nothing new
    assert (_count(conn, "agent_usage"), _count(conn, "agent_actions"), _usage_totals(conn, r.thread_id)) == before
    r.usage(1200, 1000, 20)  # incremental append
    _ingest_all(conn)
    assert _usage_totals(conn, r.thread_id) == (2, 500 + 200, 1500, 30)
    # full re-read from the start (checkpoint reset): keyed rows, identical totals
    conn.execute("UPDATE watch_checkpoints SET offset = 0, line_no = 0")
    conn.commit()
    _ingest_all(conn)
    assert _usage_totals(conn, r.thread_id) == (2, 700, 1500, 30)
    assert _count(conn, "agent_usage_duplicates") == 0


def test_partial_line_and_unknown_version(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home, version="0.200.0")
    r.user_turn()
    r.usage(100, 0, 5)
    full = json.dumps({"timestamp": "2026-09-09T12:00:10.000Z", "ordinal": r.ordinal, "type": "event_msg",
                       "payload": {"type": "token_count", "info": {
                           "total_token_usage": {"input_tokens": 300, "cached_input_tokens": 0,
                                                 "cache_write_input_tokens": 0, "output_tokens": 15,
                                                 "reasoning_output_tokens": 0, "total_tokens": 315},
                           "last_token_usage": {"input_tokens": 200, "output_tokens": 10}}}}) + "\n"
    r.append_raw(full[: len(full) // 2])
    s = _ingest_all(conn)
    assert s.usage == 1 and r.thread_id in s.unknown_version_sessions
    r.append_raw(full[len(full) // 2:])
    r.append_raw("not json\n")
    s2 = _ingest_all(conn)
    assert s2.usage == 1 and s2.unparseable == 1
    assert _usage_totals(conn, r.thread_id) == (2, 300, 0, 15)
    assert conn.execute("SELECT format_status FROM agent_sessions").fetchone()[0] == "unknown_version"


# ------------------------------------------------------------------ real records (sanitised)


def test_real_record_stale_repeat_after_abort(home: Path, conn: sqlite3.Connection) -> None:
    """Codex 0.142.3, July 2026: 6 token_count events, one repeats the previous value after
    turn_aborted. Expected figures were computed independently from the raw file."""
    install_real_fixture(home, REAL_STALE)
    s = _ingest_all(conn)
    sid = "019f4d46-6abe-74c0-84fb-3e50834bfc15"
    assert s.usage == 5
    assert _usage_totals(conn, sid) == (5, 132605, 80768, 8908)
    first = conn.execute("SELECT input_tokens, cache_read_tokens, output_tokens FROM agent_usage WHERE session_id = ?"
                         " ORDER BY at LIMIT 1", (sid,)).fetchone()
    assert tuple(first) == (87234, 10112, 3270)
    sess = conn.execute("SELECT * FROM agent_sessions").fetchone()
    assert sess["source_version"] == "0.142.3" and sess["format_status"] == "supported"
    assert json.loads(sess["usage_consistency"])["stale_usage_repeats"] == 1
    assert _count(conn, "agent_turns") == 6
    # gpt-5.5 in July 2026: the card effective then does not carry it → unpriced, never zero
    st = conn.execute("SELECT DISTINCT model, api_equiv_status, rate_card_id FROM agent_usage").fetchall()
    assert [tuple(x) for x in st] == [("gpt-5.5", "unpriced", "openai-list@2025-08-01")]


def test_real_record_parent_and_subagent(home: Path, conn: sqlite3.Connection) -> None:
    """Codex 0.153.1, September 2026: a parent thread that spawned subagents and one of
    its subagent files (session_meta.session_id is the parent's id; identity from the file
    name). Expected figures computed independently from the raw files."""
    install_real_fixture(home, REAL_PARENT)
    install_real_fixture(home, REAL_SUBAGENT)
    s = _ingest_all(conn)
    parent, child = "01a06ea7-8600-7a80-8612-a3eb93a25cea", "01a078a0-f982-7503-b280-d912d16d5d41"
    assert s.usage == 63 + 7
    assert _usage_totals(conn, parent) == (63, 527223, 6582400, 23646)
    assert _usage_totals(conn, child) == (7, 56979, 327040, 2672)
    sub = conn.execute("SELECT * FROM agent_sessions WHERE session_id = ?", (child,)).fetchone()
    assert sub["is_subagent"] == 1 and sub["parent_session_id"] == parent and sub["source_version"] == "0.153.1"
    assert json.loads(conn.execute("SELECT usage_consistency FROM agent_sessions WHERE session_id = ?",
                                   (parent,)).fetchone()[0])["stale_usage_repeats"] == 2
    # Sept 4–6 2026 is before gpt-6-astra was verified (card effective 2026-09-09): unpriced, never zero,
    # with the card that was effective at execution named. Response ids are attached to every call.
    st = conn.execute("SELECT api_equiv_status, rate_card_id, COUNT(*) n, SUM(request_id IS NOT NULL) rid"
                      " FROM agent_usage GROUP BY api_equiv_status, rate_card_id").fetchall()
    assert [tuple(x) for x in st] == [("unpriced", "openai-list@2025-08-01", 70, 70)]
    assert _count(conn, "agent_actions", "WHERE session_id = ?", parent) >= 50
    assert _count(conn, "agent_actions", "WHERE tool_name = 'spawn_agent'") == 2
    # re-import: identical
    totals = (_usage_totals(conn, parent), _usage_totals(conn, child))
    _ingest_all(conn)
    assert (_usage_totals(conn, parent), _usage_totals(conn, child)) == totals


def test_usage_without_ordinals_survives_resume_and_reimport(home: Path, conn: sqlite3.Connection) -> None:
    r = Rollout(home)
    r.user_turn()

    def strip_ordinals() -> None:
        records = [json.loads(line) for line in r.path.read_text().splitlines()]
        for record in records:
            record.pop("ordinal", None)
        r.path.write_text("".join(json.dumps(record) + "\n" for record in records))

    for _ in range(100):
        r.usage(100, 0, 10)
    strip_ordinals()
    _ingest_all(conn)
    assert _usage_totals(conn, r.thread_id) == (100, 10000, 0, 1000)
    for _ in range(5):
        r.usage(100, 0, 10)
    strip_ordinals()
    _ingest_all(conn)  # new ingestor primes previous cumulative totals
    assert _usage_totals(conn, r.thread_id) == (105, 10500, 0, 1050)
    ids = {row[0] for row in conn.execute("SELECT usage_id FROM agent_usage")}
    conn.execute("UPDATE watch_checkpoints SET offset = 0, line_no = 0")
    _ingest_all(conn)
    assert _usage_totals(conn, r.thread_id) == (105, 10500, 0, 1050)
    assert {row[0] for row in conn.execute("SELECT usage_id FROM agent_usage")} == ids
