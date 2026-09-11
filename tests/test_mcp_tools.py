"""MCP tool bodies (plain functions) and one real stdio round trip through the official SDK."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from runpeek import mcp_server, telemetry
from runpeek.agents import work
from runpeek.store import apply_schema, open_connection

FIXTURES = Path(__file__).parent / "fixtures" / "otel"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(tmp_path / "cc"))
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    monkeypatch.setenv("RUNPEEK_GEMINI_HOME", str(tmp_path / "gm"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


def _claude_events() -> list[dict]:
    out = []
    for line in (FIXTURES / "claude-code-2.1.269.jsonl").read_text().splitlines():
        out.extend(telemetry.parse_logs(json.loads(line)["body"]))
    return out


def test_create_list_attach_current_and_report(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    msg = mcp_server.task_create(conn, "Build login", "feature")
    wid = msg.split()[1]
    assert wid.startswith("wi-") and "runpeek_task_attach" in msg
    with pytest.raises(mcp_server.ToolError):
        mcp_server.task_create(conn, "", "task")
    with pytest.raises(mcp_server.ToolError):
        mcp_server.task_create(conn, "x", "epic")
    # no host identity → explicit guidance, nothing attached
    with pytest.raises(mcp_server.ToolError, match="does not tell RunPeek"):
        mcp_server.task_attach(conn, wid, "current")
    events = _claude_events()
    sid = events[0]["session_id"]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)  # what Claude Code sets for a spawned MCP server
    out = mcp_server.task_attach(conn, wid, "current")
    assert out.startswith(f"Attached session {sid[:8]} to {wid}")
    assert conn.execute("SELECT transcript_path FROM agent_sessions WHERE session_id = ?", (sid,)).fetchone()[0] \
        .startswith("telemetry://")
    # usage arriving later lands on the task
    telemetry.store_events(conn, events)
    rep = mcp_server.task_report(conn, wid)
    assert rep.splitlines()[0].startswith(f"{wid} · Build login · feature")
    assert "Accounted so far: $0.019065" in rep and "Model calls: 2/2 priced" in rep
    assert len(rep.splitlines()) < 30
    listing = mcp_server.task_list(conn)
    assert wid in listing and "Build login" in listing
    assert mcp_server.task_attach(conn, wid, "current").startswith(f"Session {sid[:8]} was already")
    # model-supplied ids are validated
    with pytest.raises(mcp_server.ToolError):
        mcp_server.task_attach(conn, "wi-;drop", "current")
    with pytest.raises(mcp_server.ToolError):
        mcp_server.task_attach(conn, wid, "no such session")


def test_attach_browser_conversation_is_unmetered_and_hashed(conn: sqlite3.Connection) -> None:
    wid = mcp_server.task_create(conn, "Plan feature", "task").split()[1]
    url = "https://chatgpt.com/c/68c0ffee-1234-4abc-9def-0123456789ab"
    out = mcp_server.task_attach_conversation(conn, wid, url, "planning chat")
    assert "unmetered" in out and "keyed hash" in out
    row = conn.execute("SELECT * FROM work_item_participants").fetchone()
    assert row["platform"] == "chatgpt" and row["metering"] == "unmetered" and row["label"] == "planning chat"
    assert "68c0ffee" not in json.dumps(dict(row))  # conversation id never stored
    again = mcp_server.task_attach_conversation(conn, wid, url)
    assert "already attached" in again
    assert conn.execute("SELECT COUNT(*) FROM work_item_participants").fetchone()[0] == 1
    other = mcp_server.task_create(conn, "Other", "task").split()[1]
    moved = mcp_server.task_attach_conversation(conn, other, url)
    assert f"moved from {wid}" in moved
    claude_url = "https://claude.ai/chat/1b2c3d4e-0000-4000-8000-000000000000"
    mcp_server.task_attach_conversation(conn, wid, claude_url)
    with pytest.raises(mcp_server.ToolError, match="unsupported"):
        mcp_server.task_attach_conversation(conn, wid, "https://example.com/x")
    rep = work.render_report(work.build_report(conn, wid))
    assert "PARTICIPANTS WITHOUT MEASURED USAGE" in rep and "claude-web" in rep and "unmetered" in rep
    assert "cost is unknown, not zero" in rep
    compact = mcp_server.task_report(conn, wid)
    assert "Unmetered participants: 1" in compact


def test_collection_health_text(conn: sqlite3.Connection) -> None:
    text = mcp_server.collection_health(conn)
    assert text.startswith("Receiver: not running") and "Claude Code:" in text and "Codex:" in text
    assert "root sessions are not attached" in text


@pytest.mark.skipif(sys.version_info < (3, 10), reason="mcp sdk")
def test_stdio_round_trip_with_official_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db = tmp_path / "n.db"
    env = dict(os.environ, RUNPEEK_DB=str(db), CLAUDE_CODE_SESSION_ID="11111111-2222-4333-8444-555555555555",
               RUNPEEK_CLAUDE_HOME=str(tmp_path / "cc"), RUNPEEK_CODEX_HOME=str(tmp_path / "cx"),
               RUNPEEK_GEMINI_HOME=str(tmp_path / "gm"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "runpeek.cli", "mcp"], env=env)

    async def run() -> tuple[list[str], str, str]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                created = await session.call_tool("runpeek_task_create", {"name": "Build login", "kind": "feature"})
                text = created.content[0].text  # type: ignore[union-attr]
                wid = text.split()[1]
                attached = await session.call_tool("runpeek_task_attach", {"task": wid})
                return names, text, attached.content[0].text  # type: ignore[union-attr]

    names, created, attached = asyncio.run(run())
    assert set(names) == {"runpeek_task_create", "runpeek_task_list", "runpeek_task_attach",
                          "runpeek_task_attach_conversation", "runpeek_task_report", "runpeek_collection_health"}
    assert created.startswith("Created wi-") and attached.startswith("Attached session 11111111")
    c = open_connection(db)
    assert c.execute("SELECT COUNT(*) FROM work_item_sessions").fetchone()[0] == 1
