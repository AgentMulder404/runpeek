"""RunPeek MCP server: the interaction surface for tasks.

Tools create tasks, attach sessions and conversations, and return compact
reports. They never expose the host's model usage; measurement arrives through
documented telemetry and transcript adapters, not through this server.

Session identity: when Claude Code spawns this server it sets
``CLAUDE_CODE_SESSION_ID`` in the server's environment (verified with Claude
Code 2.1.269 on 2026-09-11). ``session="current"`` uses that. Codex passes no
identity to MCP servers (verified with Codex CLI 0.154.0), so Codex sessions
are attached by id. Model-supplied identifiers are validated and never treated
as authorization.

Tool schemas and replies are deliberately small: every call still costs the
host tokens, and there are no per-turn "report your usage" prompts.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .agents import work
from .agents.ingest import fingerprint_key
from .agents.report import resolve_session_id
from .ids import now_iso
from .store import apply_schema, open_connection

try:  # the optional 'mcp' extra; tool annotations must resolve at module scope
    from mcp.server.mcpserver import Context, MCPServer
except ImportError:  # pragma: no cover
    Context = Any  # type: ignore[misc,assignment]
    MCPServer = None  # type: ignore[assignment,misc]

IDENT = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,200}$")
KINDS = ("task", "feature", "bugfix", "deployment")
CONVERSATION_URLS = (
    ("chatgpt", re.compile(r"^https://(chatgpt\.com|chat\.openai\.com)/(?:c|g/[^/]+/c)/([0-9a-fA-F-]{8,64})")),
    ("claude-web", re.compile(r"^https://claude\.ai/(?:chat|project/[^/]+/chat)/([0-9a-fA-F-]{8,64})")),
)


class ToolError(Exception):
    pass


def _ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ToolError(f"{what} must be 1–200 chars of letters, digits or _.:/@+-")
    return value


def db_path() -> Path:
    from .cli import resolve_db

    path, _ = resolve_db(os.environ.get("RUNPEEK_DB"))
    return path


def open_db(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(p)
    apply_schema(conn)
    return conn


def host_session() -> tuple[str, str] | None:
    """(source, session_id) for the session that launched this server, when the host says."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid and IDENT.fullmatch(sid):
        return "claude-code", sid
    return None


# --------------------------------------------------------------------------- tool bodies (plain functions, testable)


def task_create(conn: sqlite3.Connection, name: str, kind: str = "task") -> str:
    name = str(name).strip()[:120]
    if not name:
        raise ToolError("name is required")
    if kind not in KINDS:
        raise ToolError(f"kind must be one of {', '.join(KINDS)}")
    wid = work.create(conn, name, kind)
    return f"Created {wid} · {name} · {kind}. Attach this session: runpeek_task_attach(task=\"{wid}\")."


def task_list(conn: sqlite3.Connection, limit: int = 10) -> str:
    items = work.list_items(conn)[: max(1, min(int(limit), 50))]
    if not items:
        return "No tasks yet. Create one with runpeek_task_create."
    lines = []
    for it in items:
        rep = work.build_report(conn, str(it["work_item_id"]))
        cost = work._cost_cell(rep.total)
        status = it["status"] if it["status"] == "open" else f"closed/{it['outcome']}"
        lines.append(f"{it['work_item_id']}  {status:<18} {len(rep.sessions):>2} sessions  {cost:>12}  {it['name']}")
    return "\n".join(lines)


def _resolve_task(conn: sqlite3.Connection, task: str) -> str:
    got = work.resolve(conn, _ident(task, "task"))
    if isinstance(got, list):
        raise ToolError("task not found" if not got else f"task is ambiguous: {', '.join(got[:5])}")
    return got


def task_attach(conn: sqlite3.Connection, task: str, session: str = "current") -> str:
    wid = _resolve_task(conn, task)
    if session == "current":
        host = host_session()
        if host is None:
            raise ToolError("This host does not tell RunPeek which session is running. Pass the session id"
                            " (Codex: the thread id shown at start; list with `runpeek sessions --unassigned`).")
        source, sid = host
        conn.execute(
            "INSERT OR IGNORE INTO agent_sessions (session_id, source, project_path, transcript_path, first_seen_at,"
            " provider) VALUES (?,?,?,?,?,?)",
            (sid, source, os.environ.get("CLAUDE_PROJECT_DIR"), f"telemetry://{sid}", now_iso(),
             "anthropic" if source == "claude-code" else "openai"))
        conn.commit()
    else:
        got = resolve_session_id(conn, _ident(session, "session"))
        if isinstance(got, list):
            raise ToolError("session not found" if not got else "session prefix is ambiguous; use a longer prefix")
        sid = got
    result = work.assign(conn, wid, [sid])
    prev = result[0][1]
    short = sid.split("/")[-1][:8]
    if prev == wid:
        return f"Session {short} was already on {wid}."
    return f"Attached session {short} to {wid}" + (f" (moved from {prev})." if prev else ".") + \
        " Usage from this session is counted there as it is collected."


def _conversation_fp(conn: sqlite3.Connection, platform: str, conversation_id: str) -> str:
    key = fingerprint_key(conn)
    return hmac.new(key, f"{platform}:{conversation_id}".encode(), hashlib.sha256).hexdigest()[:32]


def task_attach_conversation(conn: sqlite3.Connection, task: str, url: str, label: str = "") -> str:
    wid = _resolve_task(conn, task)
    if not isinstance(url, str) or len(url) > 2048:
        raise ToolError("url is required")
    platform, conv_id = "", ""
    for name, rx in CONVERSATION_URLS:
        m = rx.match(url.strip())
        if m:
            platform, conv_id = name, m.group(m.lastindex or 1)
            break
    if not platform:
        host = urlsplit(url.strip()).hostname or "?"
        raise ToolError(f"unsupported conversation url ({host}); supported: chatgpt.com/c/<id>, claude.ai/chat/<id>")
    fp = _conversation_fp(conn, platform, conv_id)
    clean_label: str | None = re.sub(r"[\x00-\x1f\x7f]", "", str(label or ""))[:80] or None
    pid = "pt-" + secrets.token_hex(4)
    existing = conn.execute("SELECT participant_id, work_item_id FROM work_item_participants WHERE platform = ?"
                            " AND conversation_fp = ?", (platform, fp)).fetchone()
    if existing:
        conn.execute("UPDATE work_item_participants SET work_item_id = ?, label = COALESCE(?, label)"
                     " WHERE participant_id = ?", (wid, clean_label, existing["participant_id"]))
        moved = f" (moved from {existing['work_item_id']})" if existing["work_item_id"] != wid else ""
        conn.commit()
        return f"Conversation already attached to {wid}{moved}. It is unmetered: no token usage is available from" \
               f" {platform}."
    conn.execute("INSERT INTO work_item_participants (participant_id, work_item_id, platform, conversation_fp, label,"
                 " metering, attached_at) VALUES (?,?,?,?,?,'unmetered',?)",
                 (pid, wid, platform, fp, clean_label, now_iso()))
    conn.commit()
    return (f"Attached {platform} conversation to {wid} as {pid}. It is unmetered: {platform} exposes no token usage,"
            " so the report lists it as a participant without measured cost. The conversation id is stored only as a"
            " keyed hash.")


def task_report(conn: sqlite3.Connection, task: str) -> str:
    wid = _resolve_task(conn, task)
    return work.render_compact(work.build_report(conn, wid), conn)


def collection_health(conn: sqlite3.Connection) -> str:
    from . import connectors
    from .telemetry import receiver_alive

    statuses = connectors.detect(conn)
    alive = receiver_alive(conn)
    lines = ["Receiver: " + ("running" if alive else "not running — new usage is not being collected")]
    for s in statuses:
        lines.append(f"{s.label}: {s.collecting}; telemetry {s.telemetry}; MCP {s.mcp}; {s.records_found} record files")
    unassigned = conn.execute(
        "SELECT COUNT(*) FROM agent_sessions s WHERE parent_session_id IS NULL AND session_id NOT IN"
        " (SELECT session_id FROM work_item_sessions)").fetchone()[0]
    lines.append(f"{unassigned} root sessions are not attached to any task.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- server


def build_server() -> Any:
    if MCPServer is None:  # pragma: no cover - depends on the optional extra
        raise SystemExit("runpeek mcp needs the 'mcp' package: pip install 'runpeek[mcp]' (bundled in the installer)")

    server = MCPServer("runpeek", instructions="RunPeek tracks what a task costs across coding agents. Use these"
                       " tools to create a task, attach the current session or a browser conversation, and read a"
                       " compact cost report. Measurement happens outside this server.")

    def _run(fn: Any, *args: Any) -> str:
        conn = open_db()
        try:
            return str(fn(conn, *args))
        except ToolError as exc:
            return f"error: {exc}"
        except work.WorkItemError as exc:
            return f"error: {exc}"
        finally:
            conn.close()

    @server.tool(name="runpeek_task_create", description="Create a task (task|feature|bugfix|deployment).")
    async def _create(name: str, ctx: Context, kind: str = "task") -> str:
        return _run(task_create, name, kind)

    @server.tool(name="runpeek_task_list", description="List tasks with sessions and estimated cost.")
    async def _list(ctx: Context, limit: int = 10) -> str:
        return _run(task_list, limit)

    @server.tool(name="runpeek_task_attach", description="Attach a coding-agent session to a task."
                 " session='current' uses the host-provided session id when available.")
    async def _attach(task: str, ctx: Context, session: str = "current") -> str:
        return _run(task_attach, task, session)

    @server.tool(name="runpeek_task_attach_conversation", description="Attach a ChatGPT or Claude web conversation"
                 " (by its URL) to a task as an unmetered participant.")
    async def _attach_conv(task: str, url: str, ctx: Context, label: str = "") -> str:
        return _run(task_attach_conversation, task, url, label)

    @server.tool(name="runpeek_task_report", description="Compact cost report for a task.")
    async def _report(task: str, ctx: Context) -> str:
        return _run(task_report, task)

    @server.tool(name="runpeek_collection_health", description="Whether usage is actually being collected.")
    async def _health(ctx: Context) -> str:
        return _run(collection_health)

    return server


def main() -> int:
    server = build_server()
    server.run(transport="stdio")
    return 0
