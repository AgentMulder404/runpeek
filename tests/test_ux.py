"""Terminal UX: quiet feed, history vs live, dedupe, vocabulary, filters,
sanitisation, colour policy, width, and printed commands that actually parse."""

from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_fixtures import SENTINELS, Transcript
from runpeek import ui
from runpeek.agents import claude_code, diagnostics
from runpeek.agents.ingest import Ingestor
from runpeek.agents.report import render_findings, render_session, render_sessions, resolve_session_id, short_ids
from runpeek.agents.watch import Watcher
from runpeek.cli import build_parser
from runpeek.store import apply_schema, open_connection
from runpeek.ui import Term, sanitize

PROJECT = "/work/ux-project"
NOW = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "claude-home"
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(h))
    monkeypatch.delenv("NO_COLOR", raising=False)
    return h


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


def _ingest(conn: sqlite3.Connection) -> None:
    ing = Ingestor(conn)
    for tf in claude_code.discover(PROJECT):
        ing.ingest(tf)
        diagnostics.analyse_session(conn, tf.session_id)
    conn.commit()


def _watcher(conn: sqlite3.Connection, lines: list[str], **kw: object) -> Watcher:
    return Watcher(conn, project=PROJECT, history="all", interval_s=0.01, rescan_s=0.01,
                   out=lines.append, term=Term(color=False, width=80), **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ watcher feed


def test_quiet_polls_print_nothing(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("pytest")
    lines: list[str] = []
    w = _watcher(conn, lines)
    w.discover()
    w.banner()
    w.catch_up()
    n = len(lines)
    w._live = True
    w._known_at_live_start = {t.session_id}
    w.ingestor.on_event = w._on_ingest_event
    for _ in range(5):
        w.tick()
    assert len(lines) == n, lines[n:]
    # a real event still shows up; a session known before watching started is "active", not "new"
    t.user_prompt()
    t.turn_duration(12_000)
    w.tick()
    feed = "\n".join(lines[n:])
    assert "TURN STARTED" in feed and "TURN FINISHED" in feed and "12 seconds" in feed
    assert "SESSION ACTIVE" in feed and "NEW SESSION" not in feed
    assert feed.index("SESSION ACTIVE") < feed.index("TURN STARTED")
    # a session created after watching started is announced as new
    t2 = Transcript(home, PROJECT)
    t2.user_prompt()
    t2.bash("ls")
    w.discover()
    w.tick()
    assert "NEW SESSION" in "\n".join(lines[n:])
    assert not any(ln and ln.isspace() for ln in feed.splitlines())  # no whitespace-only lines
    assert "entries" not in feed and "requests" not in feed


def test_history_is_summarised_not_replayed_and_live_findings_dedupe(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(3):
        t.bash("make", is_error=True, seconds=20)
    lines: list[str] = []
    w = _watcher(conn, lines)
    w.discover()
    w.banner()
    w.catch_up()
    joined = "\n".join(lines)
    assert "Historical: 1 potential inefficiencies in 1 session" in joined
    assert "REPEATED FAILURE" not in joined  # not replayed as a live event
    w._live = True
    w.ingestor.on_event = w._on_ingest_event
    n = len(lines)
    for _ in range(3):
        t.bash("cargo build", is_error=True, seconds=20)
    w.tick()
    feed = "\n".join(lines[n:])
    assert feed.count("REPEATED FAILURE") == 1
    assert "A cargo command failed 3 times consecutively" in feed
    assert "No edit or write was observed between the failures." in feed
    assert f"Evidence: runpeek session {t.session_id[:8]}" in feed
    n = len(lines)
    t.bash("cargo build", is_error=True, seconds=20)  # grows the same item within the rate limit
    w.tick()
    assert "REPEATED FAILURE" not in "\n".join(lines[n:])
    for k in list(w._finding_updates):
        w._finding_updates[k] = 0.0  # rate limit elapsed
    t.bash("cargo build", is_error=True, seconds=20)
    w.tick()
    feed = "\n".join(lines[n:])
    assert "REPEATED FAILURE (updated)" in feed and "failed 5 times" in feed
    assert conn.execute("SELECT COUNT(*) FROM agent_findings").fetchone()[0] == 2  # make ×3 and cargo ×5


def test_missing_turn_boundaries_are_not_inferred(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.bash("ls")  # tool calls with no prompt entry and no end-of-turn record
    t.user_prompt()
    t.bash("ls")
    lines: list[str] = []
    w = _watcher(conn, lines)
    w.discover()
    w.banner()
    w.catch_up()
    w._live = True
    w.ingestor.on_event = w._on_ingest_event
    t.advance(3600)  # a long silence
    w.tick()
    assert "TURN FINISHED" not in "\n".join(lines) and "stalled" not in "\n".join(lines).lower()
    detail = render_session(conn, t.session_id, now=NOW)
    assert "no end-of-turn record in the transcript (not inferred from silence)" in detail


def test_collection_failure_and_recovery_are_visible(
    home: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("ls")
    lines: list[str] = []
    w = _watcher(conn, lines)
    w.discover()
    w.banner()
    w.catch_up()
    w._live = True
    original = w.ingestor.ingest

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(w.ingestor, "ingest", boom)
    t.bash("ls")
    w.tick()
    w.tick()
    feed = "\n".join(lines)
    assert feed.count("COLLECTION PROBLEM") == 1 and "disk unavailable" in feed
    monkeypatch.setattr(w.ingestor, "ingest", original)
    w.tick()
    assert "COLLECTION RECOVERED" in "\n".join(lines)


# ------------------------------------------------------------------- session list


def test_project_filter_mismatch_names_other_projects(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("ls")
    _ingest(conn)
    out = render_sessions(conn, "/somewhere/else", now=NOW)
    assert "No collected sessions for /somewhere/else." in out
    assert "The watcher may be collecting another project." in out
    assert "Try: runpeek sessions --all-projects" in out
    assert "Sessions have been collected for:" in out and PROJECT in out
    assert t.session_id[:8] not in out  # the filter is stated, not silently changed


def test_empty_transcripts_hidden_unless_asked(home: Path, conn: sqlite3.Connection) -> None:
    active = Transcript(home, PROJECT)
    active.user_prompt()
    active.bash("ls")
    empty = Transcript(home, PROJECT)
    empty.user_prompt()  # a prompt with no tool or model calls yet
    _ingest(conn)
    out = render_sessions(conn, PROJECT, now=NOW)
    assert active.session_id[:8] in out and empty.session_id[:8] not in out
    assert "1 empty transcript(s) hidden (--include-empty)" in out
    out2 = render_sessions(conn, PROJECT, include_empty=True, now=NOW)
    assert empty.session_id[:8] in out2 and "empty transcript (no tool or model calls yet)" in out2


def test_ambiguous_prefixes_get_longer_ids(home: Path, conn: sqlite3.Connection) -> None:
    a = Transcript(home, PROJECT, session_id="deadbeef-aaaa-4000-8000-000000000001")
    b = Transcript(home, PROJECT, session_id="deadbeef-bbbb-4000-8000-000000000002")
    for t in (a, b):
        t.user_prompt()
        t.bash("ls")
    _ingest(conn)
    ids = short_ids([a.session_id, b.session_id])
    assert ids[a.session_id] == "deadbeef-aaa" and ids[b.session_id] == "deadbeef-bbb"
    out = render_sessions(conn, PROJECT, now=NOW)
    assert "deadbeef-aaa" in out and "deadbeef-bbb" in out
    assert isinstance(resolve_session_id(conn, "deadbeef"), list)
    assert resolve_session_id(conn, "deadbeef-a") == a.session_id
    # a parent plus its own subagents is not ambiguous
    sub = Transcript(home, PROJECT, subagent_of=a.session_id)
    sub.user_prompt()
    sub.bash("ls")
    _ingest(conn)
    assert resolve_session_id(conn, "deadbeef-a") == a.session_id


def test_subagents_group_under_parent(home: Path, conn: sqlite3.Connection) -> None:
    parent = Transcript(home, PROJECT)
    parent.user_prompt()
    parent.bash("ls")
    sub = Transcript(home, PROJECT, subagent_of=parent.session_id)
    sub.user_prompt()
    sub.bash("ls")
    _ingest(conn)
    out = render_sessions(conn, PROJECT, now=NOW)
    lines = out.splitlines()
    parent_idx = next(i for i, ln in enumerate(lines) if parent.session_id[:8] in ln)
    assert lines[parent_idx + 1].startswith("  └ ") and sub.session_id[:8] in lines[parent_idx + 1]


# -------------------------------------------------------------- findings & detail


def test_finding_totals_reconcile_with_categories(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(4):
        t.bash("pytest", is_error=True, seconds=10)  # would also look like a tight loop; counted once
    for _ in range(3):
        t.read("src/a.py")
    _ingest(conn)
    rows = conn.execute("SELECT kind FROM agent_findings").fetchall()
    assert sorted(r["kind"] for r in rows) == ["repeated_failing_action", "repeated_read"]
    detail = render_session(conn, t.session_id, now=NOW)
    assert "POTENTIAL INEFFICIENCIES (2)" in detail
    assert "repeated failure 1 · repeated read 1 — each tool call is counted in at most one item" in detail


def test_finding_wording_and_no_payloads(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(4):
        t.read("package.json", seconds=30)
    _ingest(conn)
    out = render_findings(conn, PROJECT)
    flat = " ".join(out.split())  # wrapped lines joined
    assert "REPEATED READ" in out and "package.json was read 4 times in 2 minutes." in flat
    assert "No edit to that file was observed between reads." in flat
    assert "Repeated billing cannot be determined" in flat
    assert "Next: If the file is needed repeatedly" in flat and "Detail: runpeek session" in out
    for bad in ("Nothing changed", "wasted", "would save"):
        assert bad not in out
    for s in SENTINELS:
        assert s not in out


def test_unknown_and_partial_costs_render_honestly() -> None:
    assert ui.usd_cents(1) == "<$0.01" and ui.usd_cents(0) == "$0.00" and ui.usd_cents(None) == "—"
    assert ui.usd_cents(4_999_999) == "<$0.01" and ui.usd_cents(5_000_000) == "$0.01"
    assert ui.usd_exact(352_000) == "$0.000352"


# --------------------------------------------------------------- terminal policy


def test_sanitize_and_control_characters_in_labels(home: Path, conn: sqlite3.Connection) -> None:
    assert sanitize("\x1b[31mred\x1b[0m\x07bell\x9b1mX") == "red?bellX"
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("ls")
    _ingest(conn)
    conn.execute("UPDATE agent_sessions SET customer_id = ?, project_path = ?",
                 ("acme\x1b[2J\x07", PROJECT))
    conn.execute("UPDATE agent_usage SET model = ?", ("claude\x1b]0;evil\x07-5",))
    conn.commit()
    out = render_session(conn, t.session_id, now=NOW)
    assert "\x1b" not in out and "\x07" not in out and "acme?" in out


def test_color_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert Term(stream=io.StringIO()).color is False  # not a TTY

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("TERM", "xterm")
    assert Term(stream=Tty()).color is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert Term(stream=Tty()).color is False
    t = Term(color=False)
    assert t.green("ok") == "ok" and t.red("bad") == "bad"
    assert Term(color=True).amber("x") == "\x1b[33mx\x1b[0m"


def test_narrow_width_wraps(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(4):
        t.read("src/a.py", seconds=30)
    _ingest(conn)
    narrow = Term(color=False, width=40)
    out = render_findings(conn, PROJECT, term=narrow)
    wrapped = [ln for ln in out.splitlines() if ln.startswith("    Limits") or ln.startswith("      ")]
    assert wrapped and all(len(ln) <= 40 for ln in wrapped)
    assert Term(width=5).width == ui.MIN_WIDTH and Term(width=500).width == ui.MAX_WIDTH


def test_every_printed_command_parses(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(3):
        t.bash("pytest", is_error=True)
    lines: list[str] = []
    w = _watcher(conn, lines)
    w.run(once=True)
    text = "\n".join(lines) + "\n" + render_sessions(conn, PROJECT, now=NOW) + "\n"
    text += render_sessions(conn, "/none", now=NOW) + "\n" + render_session(conn, t.session_id, now=NOW)
    text += "\n" + render_findings(conn, PROJECT)
    parser = build_parser()
    cmds = re.findall(r"runpeek [a-z\-]+(?: [^\s`'\"()]+)*", text)
    assert cmds
    for cmd in cmds:
        argv = cmd.split()[1:]
        try:
            parser.parse_args(argv)
        except SystemExit as exc:  # argparse error
            raise AssertionError(f"printed command does not parse: {cmd!r}") from exc



def test_history_none_startup_wording(home: Path, conn: sqlite3.Connection) -> None:
    t = Transcript(home, PROJECT)
    t.user_prompt()
    t.bash("ls")
    lines: list[str] = []
    w = Watcher(conn, project=PROJECT, history="none", interval_s=0.01, rescan_s=0.01, out=lines.append,
                term=Term(color=False, width=80))
    w.run(once=True)
    joined = "\n".join(lines)
    assert "Ready · 1 transcript file found; existing content skipped — watching new activity only" in joined
    assert "empty" not in joined and "older than none" not in joined
