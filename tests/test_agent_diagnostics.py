from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_fixtures import Transcript
from runpeek.agents import claude_code, diagnostics
from runpeek.agents.ingest import Ingestor
from runpeek.store import apply_schema, open_connection

PROJECT = "/work/diag"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "claude-home"
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(h))
    return h


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


def _analyse(conn: sqlite3.Connection, t: Transcript) -> list[sqlite3.Row]:
    ing = Ingestor(conn)
    for tf in claude_code.discover(PROJECT):
        ing.ingest(tf)
    diagnostics.analyse_session(conn, t.session_id)
    conn.commit()
    return conn.execute("SELECT * FROM agent_findings WHERE session_id = ? ORDER BY first_at",
                        (t.session_id,)).fetchall()


def _kinds(rows: list[sqlite3.Row]) -> list[str]:
    return [r["kind"] for r in rows]


# ---------------------------------------------------------------- repeated failing action


def test_repeated_failure_without_change_is_flagged(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(3):
        t.bash("pytest tests/test_x.py", is_error=True, seconds=20)
    rows = _analyse(conn, t)
    assert _kinds(rows) == ["repeated_failing_action"]
    f = rows[0]
    counts = json.loads(f["counts"])
    assert counts["failures"] == 3 and counts["window_seconds"] > 0
    assert len(json.loads(f["evidence"])) == 3 and all(e["error"] for e in json.loads(f["evidence"]))
    assert f["severity"] == "potential_inefficiency"
    assert "A pytest command failed 3 times consecutively" in f["summary"]
    assert "commands" in f["limitations"] and "not visible" in f["limitations"]
    assert counts["usage_in_window"]["available"] is True and counts["usage_in_window"]["requests"] >= 2


def test_failure_after_edit_is_not_flagged(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("pytest", is_error=True)
    t.edit("src/a.py")
    t.bash("pytest", is_error=True)
    t.edit("src/a.py")
    t.bash("pytest", is_error=True)
    t.edit("src/a.py")
    t.bash("pytest", is_error=False)
    rows = _analyse(conn, t)
    assert "repeated_failing_action" not in _kinds(rows)  # tests after changes are not waste


def test_different_commands_failing_are_not_one_run(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("pytest a", is_error=True)
    t.bash("pytest b", is_error=True)
    t.bash("pytest c", is_error=True)
    rows = _analyse(conn, t)
    assert "repeated_failing_action" not in _kinds(rows)


# ------------------------------------------------------------------------- repeated read


def test_repeated_read_of_unchanged_file(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(4):
        t.read("src/big_module.py", seconds=30)
    rows = _analyse(conn, t)
    assert _kinds(rows) == ["repeated_read"]
    f = rows[0]
    assert "src/big_module.py" in f["summary"] and "read 4 times" in f["summary"]
    assert "Repeated billing cannot be determined" in f["limitations"]
    assert json.loads(f["counts"])["reads"] == 4


def test_reads_separated_by_edits_are_not_flagged(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(4):
        t.read("src/a.py")
        t.edit("src/a.py")
    rows = _analyse(conn, t)
    assert "repeated_read" not in _kinds(rows)


def test_reads_of_different_ranges_still_counted_but_noted(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.read("src/a.py", offset=1)
    t.read("src/a.py", offset=200)
    t.read("src/a.py", offset=400)
    rows = _analyse(conn, t)
    assert _kinds(rows) == ["repeated_read"]
    assert json.loads(rows[0]["counts"])["distinct_ranges"] == 3
    assert "Different offsets" in rows[0]["limitations"]


# --------------------------------------------------------------------------- retry loops


def test_consecutive_tool_errors_flagged(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for i in range(4):
        tid = t.tool_use("WebFetch", {"url": f"https://svc.example.com/api/{i}"}, seconds=15)
        t.tool_result(tid, is_error=True)
    rows = _analyse(conn, t)
    assert "retry_loop" in _kinds(rows)
    f = [r for r in rows if r["kind"] == "retry_loop"][0]
    assert json.loads(f["counts"])["mode"] == "consecutive_errors" and "4 errors in a row" in f["summary"]


def test_tight_repetition_flagged_even_when_successful(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(5):
        t.bash("git status", is_error=False, seconds=10)
    rows = _analyse(conn, t)
    assert "retry_loop" in _kinds(rows)
    assert json.loads([r for r in rows if r["kind"] == "retry_loop"][0]["counts"])["mode"] == "tight_repetition"


def test_silence_and_spaced_repeats_are_not_a_loop(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(5):
        t.bash("git status", is_error=False, seconds=400)  # spread over > window; user thinking in between
    t.advance(3600)  # an hour of silence
    t.user_prompt()
    t.bash("ls")
    rows = _analyse(conn, t)
    assert rows == [] or "retry_loop" not in _kinds(rows)


def test_findings_are_idempotent_and_extend(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(3):
        t.bash("pytest", is_error=True)
    rows = _analyse(conn, t)
    assert len(rows) == 1
    rows = _analyse(conn, t)  # re-analysis creates nothing new
    assert len(rows) == 1
    t.bash("pytest", is_error=True)  # one more failure → the same item is updated in place, not duplicated
    rows = _analyse(conn, t)
    assert len(rows) == 1
    assert json.loads(rows[0]["counts"])["failures"] == 4 and rows[0]["updated_at"] is not None
