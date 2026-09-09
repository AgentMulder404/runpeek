from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_fixtures import SENTINELS, Transcript, usage_block
from nemulai.agents import claude_code
from nemulai.agents.ingest import Ingestor, IngestStats
from nemulai.agents.watch import Watcher
from nemulai.store import apply_schema, open_connection

PROJECT = "/work/example-project"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "claude-home"
    monkeypatch.setenv("NEMULAI_CLAUDE_HOME", str(h))
    return h


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


def _ingest_all(conn: sqlite3.Connection, **kw: object) -> IngestStats:
    ing = Ingestor(conn, **kw)  # type: ignore[arg-type]
    stats = IngestStats()
    for tf in claude_code.discover(PROJECT):
        ing.ingest(tf, stats)
    return stats


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_discovery_and_encoding(home: Path) -> None:
    assert claude_code.encode_project_path("/Users/x/AluminatiAi") == "-Users-x-AluminatiAi"
    assert claude_code.encode_project_path("/tmp/my proj.v2") == "-tmp-my-proj-v2"
    t = Transcript(home, PROJECT)
    sub = Transcript(home, PROJECT, subagent_of=t.session_id)
    other = Transcript(home, "/work/other")
    found = claude_code.discover(PROJECT)
    ids = {tf.session_id for tf in found}
    assert t.session_id in ids and f"{t.session_id}/{sub.session_id}" in ids and other.session_id not in ids
    assert [tf for tf in found if tf.is_subagent][0].parent_session_id == t.session_id
    assert len(claude_code.discover(None, all_projects=True)) == 3


def test_subagent_turns_do_not_collide_with_parent(home: Path, conn: sqlite3.Connection) -> None:
    parent = Transcript(home, PROJECT)
    pid = parent.user_prompt()
    parent.bash("pytest")
    sub = Transcript(home, PROJECT, subagent_of=parent.session_id)
    sub.prompt_id = pid
    e = sub._base("user")
    e.update(promptId=pid, isSidechain=True, message={"role": "user", "content": "SECRET_PROMPT_TEXT"})
    sub.append(e)
    sub.bash("ls")
    _ingest_all(conn)
    turns = conn.execute("SELECT session_id, turn_id FROM agent_turns ORDER BY session_id").fetchall()
    assert len(turns) == 2 and len({r["session_id"] for r in turns}) == 2
    assert any(r["turn_id"] == pid for r in turns) and any(r["turn_id"].startswith(pid + "@") for r in turns)


def test_bounded_history_policies(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("pytest")
    old = datetime.now(timezone.utc) - timedelta(days=30)
    os.utime(t.path, (old.timestamp(), old.timestamp()))
    # since 7d: an old file is skipped entirely, but its checkpoint is set at the end
    s = _ingest_all(conn, history_since=datetime.now(timezone.utc) - timedelta(days=7))
    assert s.skipped_history == 1 and _count(conn, "agent_actions") == 0
    cp = conn.execute("SELECT offset, size FROM watch_checkpoints").fetchone()
    assert cp["offset"] == cp["size"]
    # new lines after that are still ingested
    t.bash("pytest")
    _ingest_all(conn, history_since=datetime.now(timezone.utc) - timedelta(days=7))
    assert _count(conn, "agent_actions") == 1
    # 'none' on a fresh store skips existing content
    c2 = open_connection(home / "n2.db")
    apply_schema(c2)
    ing = Ingestor(c2, history_none=True)
    for tf in claude_code.discover(PROJECT):
        ing.ingest(tf)
    assert _count(c2, "agent_actions") == 0
    # 'all' ingests everything
    c3 = open_connection(home / "n3.db")
    apply_schema(c3)
    ing = Ingestor(c3)
    for tf in claude_code.discover(PROJECT):
        ing.ingest(tf)
    assert _count(c3, "agent_actions") == 2


def test_incremental_ingest_and_partial_lines(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    tid = t.tool_use("Bash", {"command": "make"})
    s1 = _ingest_all(conn)
    assert s1.actions == 1 and s1.usage == 1
    assert conn.execute("SELECT is_error FROM agent_actions").fetchone()["is_error"] is None
    # a partially written result line is not consumed
    t.advance(1)
    e = t._base("user")
    e.update(message={"role": "user", "content": [{"tool_use_id": tid, "type": "tool_result", "content": "x",
                                                    "is_error": True}]})
    line = json.dumps(e) + "\n"
    t.append_raw(line[: len(line) // 2])
    s2 = _ingest_all(conn)
    assert s2.entries == 0
    assert conn.execute("SELECT is_error FROM agent_actions").fetchone()["is_error"] is None
    t.append_raw(line[len(line) // 2 :])
    s3 = _ingest_all(conn)
    assert s3.entries == 1
    assert conn.execute("SELECT is_error FROM agent_actions").fetchone()["is_error"] == 1


def test_restart_does_not_duplicate(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("pytest")
    t.read("src/a.py")
    _ingest_all(conn)
    counts = {tbl: _count(conn, tbl) for tbl in ("agent_actions", "agent_usage", "agent_turns")}
    _ingest_all(conn)  # a fresh Ingestor over the same store
    assert {tbl: _count(conn, tbl) for tbl in counts} == counts
    # force a full re-read (checkpoint reset) — keys make it idempotent
    conn.execute("UPDATE watch_checkpoints SET offset = 0, line_no = 0")
    conn.commit()
    _ingest_all(conn)
    assert {tbl: _count(conn, tbl) for tbl in counts} == counts


def test_truncation_and_rotation(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("pytest")
    t.bash("pytest")
    _ingest_all(conn)
    assert _count(conn, "agent_actions") == 2
    # truncation: file rewritten shorter with one of the same tool_use ids and a new one
    lines = t.path.read_text().splitlines(keepends=True)
    t.path.write_text("".join(lines[:2]))
    t.bash("make")
    s = _ingest_all(conn)
    assert s.reset_files == 1
    assert _count(conn, "agent_actions") == 3  # 2 old (kept, keyed) + 1 new; nothing duplicated
    # rotation: same path, new inode
    content = t.path.read_text()
    t.path.unlink()
    t.path.write_text(content)
    t.bash("ls")
    s = _ingest_all(conn)
    assert s.reset_files == 1 and _count(conn, "agent_actions") == 4


def test_concurrent_sessions_interleaved(home: Path, conn: sqlite3.Connection) -> None:
    a = Transcript(home, PROJECT)
    b = Transcript(home, PROJECT)
    a.user_prompt()
    b.user_prompt()
    for _ in range(3):
        a.bash("pytest")
        b.read("x.py")
    _ingest_all(conn)
    rows = conn.execute("SELECT session_id, COUNT(*) n FROM agent_actions GROUP BY session_id").fetchall()
    assert {r["session_id"]: r["n"] for r in rows} == {a.session_id: 3, b.session_id: 3}
    assert _count(conn, "agent_sessions") == 2


def test_streamed_entries_share_message_id_and_count_once(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.tool_use("Bash", {"command": "pytest"}, usage=usage_block(inp=1000, out=50, cw5=300, cw1=100, cr=5000),
               repeat_entries=3)
    s = _ingest_all(conn)
    assert s.usage == 1 and _count(conn, "agent_usage") == 1
    u = conn.execute("SELECT * FROM agent_usage").fetchone()
    assert (u["input_tokens"], u["output_tokens"], u["cache_write_5m_tokens"], u["cache_write_1h_tokens"],
            u["cache_read_tokens"]) == (1000, 50, 300, 100, 5000)
    assert u["usage_kind"] == "per_request" and u["provenance"] == "provider_reported"
    assert u["source_cost_nanos"] is None  # transcripts never carry cost
    # api-equivalent at opus-5 list: 1000×5 + 300×6.25 + 100×10 + 5000×0.5 + 50×25 per M
    assert u["api_equiv_nanos"] == 5_000_000 + 1_875_000 + 1_000_000 + 2_500_000 + 1_250_000
    assert u["api_equiv_status"] == "priced" and u["rate_card_id"] == "anthropic-list@2026-09-09"


def test_missing_usage_and_unknown_model(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.assistant([{"type": "text", "text": "SECRET_ASSISTANT_TEXT"}], usage=None)  # no usage block at all
    t.assistant([{"type": "text", "text": "SECRET_ASSISTANT_TEXT"}], usage=usage_block(), model="claude-unknown-9")
    _ingest_all(conn)
    rows = conn.execute("SELECT model, api_equiv_status, api_equiv_nanos FROM agent_usage ORDER BY at").fetchall()
    assert len(rows) == 1  # an entry without usage produces no usage row (nothing to count)
    assert rows[0]["api_equiv_status"] == "unpriced" and rows[0]["api_equiv_nanos"] is None


def test_unknown_writer_version_and_unparseable_lines(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT, version="3.4.0")
    t.user_prompt()
    t.bash("pytest")
    t.append_raw("this is not json\n")
    s = _ingest_all(conn)
    assert s.unparseable == 1 and t.session_id in s.unknown_version_sessions
    row = conn.execute("SELECT format_status, source_version, entries_unparseable FROM agent_sessions").fetchone()
    assert row["format_status"] == "unknown_version" and row["source_version"] == "3.4.0"
    assert row["entries_unparseable"] == 1
    assert _count(conn, "agent_actions") == 1  # best-effort parsing still worked


def test_privacy_nothing_content_like_is_stored(home: Path, conn: sqlite3.Connection, tmp_path: Path) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("curl -H 'Authorization: SECRET_COMMAND_PAYLOAD' https://x", is_error=True)
    t.bash("curl -H 'Authorization: SECRET_COMMAND_PAYLOAD' https://x", is_error=True)
    t.bash("curl -H 'Authorization: SECRET_COMMAND_PAYLOAD' https://x", is_error=True)
    t.read("secret_dir/notes.md")
    t.edit("secret_dir/notes.md")
    t.tool_use("WebFetch", {"url": "https://user:SECRET_COMMAND_PAYLOAD@example.com/p?token=SECRET_COMMAND_PAYLOAD"})
    _ingest_all(conn)
    from nemulai.agents import diagnostics

    diagnostics.analyse_session(conn, t.session_id)
    conn.commit()
    dump = []
    for tbl in ("agent_sessions", "agent_turns", "agent_actions", "agent_usage", "agent_findings", "watch_checkpoints"):
        for r in conn.execute(f"SELECT * FROM {tbl}"):
            dump.append(json.dumps(dict(r)))
    text = "\n".join(dump)
    for s in SENTINELS:
        assert s not in text, s
    assert "Authorization" not in text and "token=" not in text and "user:" not in text
    # allowlisted metadata IS present
    assert '"target": "curl"' in text and '"target": "secret_dir/notes.md"' in text
    assert '"target": "example.com"' in text
    # and the rendered reports carry none of it either
    from nemulai.agents.report import render_session, render_sessions

    rendered = render_sessions(conn, PROJECT) + render_session(conn, t.session_id)
    for s in SENTINELS:
        assert s not in rendered


def test_subscription_labelling_in_reports(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.text()
    _ingest_all(conn)
    from nemulai.agents.report import render_session, render_sessions

    for txt in (render_sessions(conn, PROJECT), render_session(conn, t.session_id)):
        assert "API-equivalent" in txt and "not a subscription charge" in txt
    assert "source-reported cost: not available" in render_session(conn, t.session_id)


def test_watcher_run_once_and_clean_stop(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("pytest")
    lines: list[str] = []
    w = Watcher(conn, project=PROJECT, history="all", interval_s=0.05, rescan_s=0.05, out=lines.append)
    totals = w.run(once=True)
    assert totals.actions == 1 and any("source claude-code" in ln for ln in lines)
    assert any("allowlisted metadata only" in ln for ln in lines)
    # continuous mode: stop from another thread, new session discovered meanwhile.
    # The thread opens its own connection: sqlite3 connections are thread-bound.
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    holder: dict[str, Watcher] = {}
    ready = threading.Event()

    def run_in_thread() -> None:
        c2 = open_connection(db_path)
        w = Watcher(c2, project=PROJECT, history="all", interval_s=0.05, rescan_s=0.05, out=lines.append)
        holder["w"] = w
        ready.set()
        w.run()
        c2.close()

    th = threading.Thread(target=run_in_thread)
    th.start()
    ready.wait(5)
    t2 = Transcript(home, PROJECT)
    t2.user_prompt()
    t2.bash("make")
    deadline = threading.Event()
    for _ in range(100):
        if _count(conn, "agent_actions") >= 2:
            break
        deadline.wait(0.05)
    holder["w"].stop.set()
    th.join(5)
    assert not th.is_alive()
    assert _count(conn, "agent_actions") == 2
    assert any(ln.startswith("watch stopped") for ln in lines)
    cp = conn.execute("SELECT COUNT(*) FROM watch_checkpoints").fetchone()[0]
    assert cp == 2
