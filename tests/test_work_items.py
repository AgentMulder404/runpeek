"""Work items: creation, explicit assignment and reassignment, subagent inclusion,
cross-agent aggregation, coverage reporting, idempotent re-import, CLI flow."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_fixtures import SENTINELS as CC_SENTINELS
from agent_fixtures import Transcript, usage_block
from codex_fixtures import CWD, Rollout
from codex_fixtures import SENTINELS as CX_SENTINELS
from runpeek.agents import claude_code, codex, work
from runpeek.agents.ingest import Ingestor, IngestStats
from runpeek.store import apply_schema, open_connection

PROJECT = CWD
SEPT = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    cc, cx = tmp_path / "claude-home", tmp_path / "codex-home"
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(cc))
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(cx))
    codex._head_cache.clear()
    return cc, cx


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


def _ingest(conn: sqlite3.Connection) -> IngestStats:
    ing = Ingestor(conn)
    stats = IngestStats()
    for tf in claude_code.discover(PROJECT) + codex.discover(PROJECT):
        ing.ingest(tf, stats)
    return stats


def _feature(conn: sqlite3.Connection, homes: tuple[Path, Path]) -> dict[str, str]:
    """The acceptance scenario: one feature across Claude Code (with a subagent) and Codex
    (with a retry), all in September 2026 so every model is priced."""
    cc, cx = homes
    t1 = Transcript(cc, PROJECT, start=SEPT)
    t1.user_prompt()
    tid = t1.tool_use("Bash", {"command": "pytest"}, usage=usage_block(inp=1000, out=100, cw5=0, cw1=0, cr=0))
    t1.tool_result(tid, is_error=True)  # opus-5: 1000×5 + 100×25 per M, twice
    t1.text(usage=usage_block(inp=1000, out=100, cw5=0, cw1=0, cr=0))
    sub = Transcript(cc, PROJECT, subagent_of=t1.session_id, start=SEPT)
    sub.user_prompt()
    sub.text(usage=usage_block(inp=500, out=50, cw5=0, cw1=0, cr=2000))  # 500×5 + 2000×0.5 + 50×25
    r1 = Rollout(cx, model="gpt-5", start=SEPT.replace(hour=11))
    r1.user_turn()
    r1.exec("pytest", exit_code=1)
    r1.usage(2000, 1000, 100)  # gpt-5: 1000×1.25 + 1000×0.125 + 100×10
    r1.abort()
    r1.user_turn()  # the retry
    r1.usage(0, 0, 0, repeat_stale=True)
    r1.usage(3000, 2000, 200)  # 1000×1.25 + 2000×0.125 + 200×10
    r1.complete()
    unrelated = Transcript(cc, PROJECT, start=SEPT.replace(hour=15))
    unrelated.user_prompt()
    unrelated.text(usage=usage_block())
    _ingest(conn)
    return {"cc": t1.session_id, "sub": f"{t1.session_id}/{sub.session_id}", "cx": r1.thread_id,
            "unrelated": unrelated.session_id}


EXPECTED_CC = 2 * (1000 * 5_000 + 100 * 25_000)
EXPECTED_SUB = 500 * 5_000 + 2000 * 500 + 50 * 25_000
EXPECTED_CX = (1000 * 1_250 + 1000 * 125 + 100 * 10_000) + (1000 * 1_250 + 2000 * 125 + 200 * 10_000)


def test_acceptance_scenario_groups_cross_agent_activity(conn: sqlite3.Connection, homes: tuple[Path, Path]) -> None:
    ids = _feature(conn, homes)
    wid = work.create(conn, "Add export endpoint", "feature", repository=PROJECT, branch="main", issue="#42")
    work.assign(conn, wid, [ids["cc"], ids["cx"]])  # the subagent follows its parent
    r = work.build_report(conn, wid)
    assert {ln.session_id for ln in r.sessions} == {ids["cc"], ids["sub"], ids["cx"]}
    assert r.subagent_count == 1 and r.explicit_count == 2
    assert r.total.calls == 5 and r.total.priced_calls == 5 and r.coverage == "complete"
    assert r.total.cost_nanos == EXPECTED_CC + EXPECTED_SUB + EXPECTED_CX
    assert r.by_agent["claude-code"].cost_nanos == EXPECTED_CC + EXPECTED_SUB
    assert r.by_agent["codex"].cost_nanos == EXPECTED_CX
    assert set(r.by_model) == {("claude-code", "claude-opus-5"), ("codex", "gpt-5")}
    assert r.timeline_unit == "hour" and len(r.timeline) == 2
    assert r.item["outcome"] is None and "in progress" in work.outcome_line(r.item)
    text = work.render_report(r, trace=10, conn=conn)
    assert "estimated model cost (API-equivalent, list prices)" in text
    assert "Model usage only" in text and "not infrastructure" in text
    assert "all 5 model calls priced" in text and "coverage complete" in text
    assert "BY AGENT" in text and "Claude Code" in text and "Codex" in text
    assert "subagent of" in text and "1 subagent included via parent" in text
    assert "1 repeated usage snapshots ignored" in text
    assert "anthropic-list@2026-09-09" in text and "openai-list@2026-09-10" in text
    assert "SOURCE EVENTS" in text and f"{ids['cx']}:" in text  # traceable usage ids
    for s in CC_SENTINELS + CX_SENTINELS:
        assert s not in text
    # a pin re-prices only its own provider's calls; Anthropic calls keep their stored estimates
    pinned = work.build_report(conn, wid, pin="openai-list@2026-09-10")
    assert pinned.total.priced_calls == 5 and pinned.total.cost_nanos == r.total.cost_nanos
    # re-import leaves totals unchanged
    _ingest(conn)
    r2 = work.build_report(conn, wid)
    assert r2.total.cost_nanos == r.total.cost_nanos and r2.total.calls == r.total.calls
    # the unrelated session stays visible as unassigned
    from runpeek.agents.report import render_sessions

    listing = render_sessions(conn, PROJECT, unassigned=True)
    assert ids["unrelated"][:8] in listing and ids["cc"][:8] not in listing


def test_outcome_and_status_transitions(conn: sqlite3.Connection, homes: tuple[Path, Path]) -> None:
    wid = work.create(conn, "Fix login bug", "bugfix")
    with pytest.raises(work.WorkItemError):
        work.set_status(conn, wid, status="closed")  # needs an outcome
    with pytest.raises(work.WorkItemError):
        work.set_status(conn, wid, outcome="done")
    for outcome in ("completed", "incomplete", "failed", "abandoned"):
        work.set_status(conn, wid, status="closed", outcome=outcome)
        it = work.get(conn, wid)
        assert it is not None and it["status"] == "closed" and it["outcome"] == outcome and it["closed_at"]
    work.set_status(conn, wid, status="open")
    it = work.get(conn, wid)
    assert it is not None and it["status"] == "open" and it["outcome"] is None and it["closed_at"] is None
    with pytest.raises(work.WorkItemError):
        work.create(conn, "x", "epic")
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM work_item_events WHERE work_item_id = ?", (wid,))]
    assert kinds[0] == "created" and kinds.count("status") == 5


def test_assignment_reassignment_and_no_double_counting(conn: sqlite3.Connection, homes: tuple[Path, Path]) -> None:
    ids = _feature(conn, homes)
    a = work.create(conn, "A", "task")
    b = work.create(conn, "B", "task")
    assert work.assign(conn, a, [ids["cc"]]) == [(ids["cc"], None)]
    assert work.assign(conn, a, [ids["cc"]]) == [(ids["cc"], a)]  # no-op
    assert work.build_report(conn, a).total.cost_nanos == EXPECTED_CC + EXPECTED_SUB
    # reassign the parent: the subagent follows, nothing is counted twice
    assert work.assign(conn, b, [ids["cc"]]) == [(ids["cc"], a)]
    ra, rb = work.build_report(conn, a), work.build_report(conn, b)
    assert ra.total.calls == 0 and rb.total.cost_nanos == EXPECTED_CC + EXPECTED_SUB
    # explicitly moving the subagent elsewhere removes it from the parent's item
    work.assign(conn, a, [ids["sub"]])
    ra, rb = work.build_report(conn, a), work.build_report(conn, b)
    assert ra.total.cost_nanos == EXPECTED_SUB and rb.total.cost_nanos == EXPECTED_CC
    assert [ln.how for ln in ra.sessions] == ["explicit"]
    # every usage row is counted under exactly one item
    counted = sum(r.total.calls for r in (ra, rb))
    assert counted == 3 and conn.execute("SELECT COUNT(*) FROM work_item_sessions").fetchone()[0] == 2
    assert work.unassign(conn, [ids["sub"], ids["cx"]]) == [(ids["sub"], a), (ids["cx"], None)]
    assert work.build_report(conn, b).total.cost_nanos == EXPECTED_CC + EXPECTED_SUB
    events = [r["kind"] for r in conn.execute("SELECT kind FROM work_item_events ORDER BY id")]
    assert "reassigned" in events and "unassigned" in events
    with pytest.raises(work.WorkItemError):
        work.assign(conn, a, ["nope"])


def test_suggestions_never_assign(conn: sqlite3.Connection, homes: tuple[Path, Path]) -> None:
    ids = _feature(conn, homes)
    wid = work.create(conn, "Suggest me", "task", repository=PROJECT, branch="main")
    rows = work.suggest(conn, wid)
    suggested = {str(r["session_id"]) for r, _ in rows}
    assert ids["cc"] in suggested and ids["cx"] in suggested and ids["sub"] not in suggested
    why = dict((str(r["session_id"]), w) for r, w in rows)
    assert "same project path" in why[ids["cc"]] and "branch main" in why[ids["cc"]]
    assert conn.execute("SELECT COUNT(*) FROM work_item_sessions").fetchone()[0] == 0
    text = work.render_suggestions(conn, wid)
    assert "Nothing is assigned by this command" in text
    work.assign(conn, wid, [ids["cc"]])
    assert ids["cc"] not in {str(r["session_id"]) for r, _ in work.suggest(conn, wid)}


def test_unpriced_missing_usage_and_unknown_format_are_visible(conn: sqlite3.Connection,
                                                              homes: tuple[Path, Path]) -> None:
    cc, cx = homes
    t = Transcript(cc, PROJECT, version="9.9.9", start=SEPT)
    t.user_prompt()
    t.assistant([{"type": "text", "text": "SECRET_ASSISTANT_TEXT"}],
                usage=usage_block(inp=100, out=10, cw5=0, cw1=0, cr=0), model="claude-unknown-9")
    t.assistant([{"type": "text", "text": "SECRET_ASSISTANT_TEXT"}], usage=None)
    r = Rollout(cx, model="gpt-5.5", start=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc))  # before verification
    r.user_turn()
    r.usage(100, 0, 10)
    r2 = Rollout(cx, model="gpt-5", start=SEPT)
    r2.user_turn()
    r2.usage(1000, 0, 10)
    _ingest(conn)
    wid = work.create(conn, "Gaps", "task")
    work.assign(conn, wid, [t.session_id, r.thread_id, r2.thread_id])
    rep = work.build_report(conn, wid)
    assert rep.total.calls == 3 and rep.total.priced_calls == 1 and rep.coverage == "partial"
    assert rep.total.cost_nanos == 1000 * 1_250 + 10 * 10_000
    assert rep.unpriced_reasons == {"model claude-unknown-9 not in anthropic-list@2026-09-09": 1,
                                    "model gpt-5.5 not in openai-list@2025-08-01": 1}
    text = work.render_report(rep)
    assert "1 of 3 model calls priced" in text and "priced subtotal" in text
    assert "unsupported record format" in text or "unknown version" in text
    assert "model gpt-5.5 not in openai-list@2025-08-01" in text
    # pinned view prices the July call under today's card, and says so, without touching stored rows
    pinned = work.build_report(conn, wid, pin="openai-list@2026-09-10")
    assert pinned.total.priced_calls == 2 and "pinned rate card openai-list@2026-09-10" in work.render_report(pinned)
    assert conn.execute("SELECT COUNT(*) FROM agent_usage WHERE api_equiv_status = 'priced'").fetchone()[0] == 1


def test_empty_work_item_report(conn: sqlite3.Connection) -> None:
    wid = work.create(conn, "Nothing yet", "deployment", deployment="prod-2026-09-10")
    text = work.render_report(work.build_report(conn, wid))
    assert "No model calls recorded" in text and "deployment prod-2026-09-10" in text
    assert "not infrastructure" in text
    assert work.resolve(conn, wid) == wid and work.resolve(conn, "Nothing yet") == wid
    assert work.resolve(conn, wid[3:6]) == wid and work.resolve(conn, "zzz") == []


# ------------------------------------------------------------------ CLI


def _run(args: list[str], *, cwd: Path, homes: tuple[Path, Path]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["RUNPEEK_CLAUDE_HOME"], env["RUNPEEK_CODEX_HOME"] = str(homes[0]), str(homes[1])
    env["NO_COLOR"] = "1"
    return subprocess.run([sys.executable, "-m", "runpeek.cli", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120)


def test_cli_work_flow(tmp_path: Path, homes: tuple[Path, Path]) -> None:
    db = tmp_path / "n.db"
    conn = open_connection(db)
    apply_schema(conn)
    ids = _feature(conn, homes)
    conn.close()
    w = _run(["watch", "--db", str(db), "--project", PROJECT, "--history", "all", "--once"], cwd=tmp_path, homes=homes)
    assert w.returncode == 0 and "Watching Claude Code and Codex in codex-project" in w.stdout, w.stderr

    n = _run(["work", "new", "Add export endpoint", "--kind", "feature", "--db", str(db), "--repository", PROJECT,
              "--issue", "#42"], cwd=tmp_path, homes=homes)
    assert n.returncode == 0 and n.stdout.startswith("created wi-"), n.stderr
    wid = n.stdout.split()[1]

    sg = _run(["work", "suggest", wid, "--db", str(db)], cwd=tmp_path, homes=homes)
    assert sg.returncode == 0 and "same project path" in sg.stdout and "Nothing is assigned" in sg.stdout

    a = _run(["work", "assign", wid, ids["cc"][:8], ids["cx"][:8], "--db", str(db)], cwd=tmp_path, homes=homes)
    assert a.returncode == 0 and a.stdout.count("assigned to") == 2, a.stderr

    ls = _run(["work", "list", "--db", str(db)], cwd=tmp_path, homes=homes)
    assert ls.returncode == 0 and wid in ls.stdout and "feature" in ls.stdout and "Add export endpoint" in ls.stdout

    s = _run(["work", "status", wid, "--outcome", "completed", "--db", str(db)], cwd=tmp_path, homes=homes)
    assert s.returncode == 0 and "completed (closed" in s.stdout

    sh = _run(["work", "show", wid, "--trace", "--db", str(db)], cwd=tmp_path, homes=homes)
    assert sh.returncode == 0, sh.stderr
    out = sh.stdout
    assert "WORK ITEM " + wid in out and "Outcome: completed" in out and "issue #42" in out
    assert "ESTIMATED MODEL COST" in out and "all 5 model calls priced" in out
    assert "BY AGENT" in out and "BY MODEL" in out and "BY SESSION" in out and "TIMELINE" in out
    assert "ACCOUNTING COVERAGE" in out and "PRICING PROVENANCE" in out and "SOURCE EVENTS" in out
    for sentinel in CC_SENTINELS + CX_SENTINELS:
        assert sentinel not in out

    ses = _run(["sessions", "--db", str(db), "--project", PROJECT], cwd=tmp_path, homes=homes)
    assert "RECENT CODING-AGENT SESSIONS" in ses.stdout and wid in ses.stdout and "(via parent)" in ses.stdout
    un = _run(["sessions", "--db", str(db), "--project", PROJECT, "--unassigned"], cwd=tmp_path, homes=homes)
    assert ids["unrelated"][:8] in un.stdout and ids["cc"][:8] not in un.stdout

    d = _run(["session", ids["cx"][:8], "--db", str(db)], cwd=tmp_path, homes=homes)
    assert d.returncode == 0 and f"Work item: {wid}" in d.stdout and "Agent: Codex" in d.stdout
    assert "not available in Codex session records" in d.stdout

    x = _run(["export", "--db", str(db), "--out", str(tmp_path / "x.jsonl")], cwd=tmp_path, homes=homes)
    assert x.returncode == 0
    kinds = {line.split('"record":"')[1].split('"')[0] for line in (tmp_path / "x.jsonl").read_text().splitlines()}
    assert {"work_items", "work_item_sessions", "work_item_events", "agent_usage"} <= kinds

    bad = _run(["work", "status", wid, "--status", "closed", "--outcome", "shipped", "--db", str(db)], cwd=tmp_path,
               homes=homes)
    assert bad.returncode != 0
