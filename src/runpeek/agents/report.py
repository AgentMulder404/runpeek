"""Session list, session detail and findings — the review surfaces.

Every cost shown here is an API-equivalent estimate at list prices. The
explanation travels with the number; it is never shown bare.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .. import ui
from ..ui import Term, sanitize
from .diagnostics import title_for
from .ingest import ADAPTERS
from .work import assignment_map

COST_NOTE = ("API-equivalent estimate at list prices. Not your subscription charge, quota usage, or proven "
             "savings. Usage is as reported in agent session records, not independently verified billing.")


def records_label(source: str | None) -> str:
    a = ADAPTERS.get(str(source))
    return str(a.RECORDS_LABEL) if a else "agent session records"


def agent_label(source: str | None) -> str:
    a = ADAPTERS.get(str(source))
    return str(a.LABEL) if a else str(source or "?")


def cost_note(source: str | None) -> str:
    return COST_NOTE.replace("agent session records", records_label(source))


# --------------------------------------------------------------------------- queries


def _session_rows(conn: sqlite3.Connection, project: str | None, limit: int | None) -> list[sqlite3.Row]:
    where = "" if project is None else " WHERE s.project_path = ?"
    params: tuple[Any, ...] = () if project is None else (project,)
    lim = "" if limit is None else f" LIMIT {int(limit)}"
    return conn.execute(
        "SELECT s.*,"
        " (SELECT MIN(started_at) FROM agent_turns t WHERE t.session_id = s.session_id) AS first_turn_at,"
        " (SELECT COUNT(*) FROM agent_turns t WHERE t.session_id = s.session_id) AS turns,"
        " (SELECT COUNT(*) FROM agent_actions a WHERE a.session_id = s.session_id) AS actions,"
        " (SELECT COUNT(*) FROM agent_actions a WHERE a.session_id = s.session_id AND a.is_error = 1) AS errors,"
        " (SELECT COUNT(*) FROM agent_usage u WHERE u.session_id = s.session_id) AS requests,"
        " (SELECT COALESCE(SUM(input_tokens),0) FROM agent_usage u WHERE u.session_id = s.session_id) AS inp,"
        " (SELECT COALESCE(SUM(cache_read_tokens),0) FROM agent_usage u WHERE u.session_id = s.session_id) AS cr,"
        " (SELECT COALESCE(SUM(COALESCE(cache_write_5m_tokens,0)+COALESCE(cache_write_1h_tokens,0)),0)"
        "    FROM agent_usage u WHERE u.session_id = s.session_id) AS cw,"
        " (SELECT COALESCE(SUM(output_tokens),0) FROM agent_usage u WHERE u.session_id = s.session_id) AS outp,"
        " (SELECT COALESCE(SUM(api_equiv_nanos),0) FROM agent_usage u WHERE u.session_id = s.session_id"
        "    AND api_equiv_status = 'priced') AS equiv,"
        " (SELECT COUNT(*) FROM agent_usage u WHERE u.session_id = s.session_id"
        "    AND api_equiv_status != 'priced') AS unpriced,"
        " (SELECT COUNT(*) FROM agent_findings f WHERE f.session_id = s.session_id) AS findings"
        f" FROM agent_sessions s{where}"
        f" ORDER BY COALESCE(s.first_event_at, s.first_seen_at) DESC{lim}",
        params,
    ).fetchall()


def known_projects(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT project_path, COUNT(*) n FROM agent_sessions WHERE project_path IS NOT NULL"
        " GROUP BY project_path ORDER BY n DESC"
    ).fetchall()
    return [(str(r["project_path"]), int(r["n"])) for r in rows]


def _started(r: sqlite3.Row) -> str | None:
    v = r["first_turn_at"] or r["first_event_at"] or r["first_seen_at"]
    return str(v) if v else None


def _empty(r: sqlite3.Row) -> bool:
    return int(r["actions"]) == 0 and int(r["requests"]) == 0


def _state(r: sqlite3.Row) -> str:
    if r["format_status"] == "unknown_version":
        return "unsupported transcript version"
    if r["format_status"] == "unparseable":
        return "unparseable transcript"
    if _empty(r):
        return "empty transcript (no tool or model calls yet)"
    return ""


def short_ids(ids: list[str]) -> dict[str, str]:
    """Shortest prefix (≥ 8) that keeps the displayed ids distinct."""
    leaves = {sid: sid.split("/")[-1] for sid in ids}
    n = 8
    while n < 36:
        shorts = {sid: leaf[:n] for sid, leaf in leaves.items()}
        if len(set(shorts.values())) == len(shorts):
            return shorts
        n += 4
    return leaves


def resolve_session_id(conn: sqlite3.Connection, prefix: str) -> str | list[str]:
    """The unique session matching a prefix, or the list of candidates."""
    rows = conn.execute(
        "SELECT session_id FROM agent_sessions WHERE session_id = ? OR session_id LIKE ? OR session_id LIKE ?"
        " ORDER BY session_id",
        (prefix, f"{prefix}%", f"%/{prefix}%"),
    ).fetchall()
    ids = [str(r["session_id"]) for r in rows]
    if prefix in ids:
        return prefix
    if len(ids) == 1:
        return ids[0]
    # A main session plus only its own subagents is not ambiguous: the parent wins.
    mains = [i for i in ids if "/" not in i]
    if len(mains) == 1 and all(i == mains[0] or i.startswith(mains[0] + "/") for i in ids):
        return mains[0]
    return ids


# ----------------------------------------------------------------------- sessions


def render_sessions(conn: sqlite3.Connection, project: str | None, last: int = 20, *, include_empty: bool = False,
                    detailed: bool = False, unassigned: bool = False, term: Term | None = None,
                    now: datetime | None = None) -> str:
    term = term or Term(color=False)
    rows = _session_rows(conn, project, None)
    assigned = assignment_map(conn)
    shown = [r for r in rows if include_empty or not _empty(r)]
    if unassigned:
        shown = [r for r in shown if str(r["session_id"]) not in assigned]
    hidden_empty = len(rows) - len(shown) if not unassigned else 0
    shown = shown[:last]
    L: list[str] = []
    L.append(term.bold("RECENT CODING-AGENT SESSIONS" + (" — NOT ASSIGNED TO A WORK ITEM" if unassigned else "")))
    if project:
        L.append(f"Project: {ui.project_name(project)} ({sanitize(project)})")
    else:
        L.append("Project: all projects")
    L.append(f"Newest first · showing {len(shown)}" + (f" of {len(rows)}" if len(rows) != len(shown) else "")
             + (f" · {hidden_empty} empty transcript(s) hidden (--include-empty)" if hidden_empty else ""))
    L.append("")
    if not shown:
        if unassigned:
            L.append("Every collected session is assigned to a work item.")
            return "\n".join(L)
        if project:
            L.append(f"No collected sessions for {sanitize(project)}.")
            L.append("The watcher may be collecting another project.")
            L.append("Try: runpeek sessions --all-projects")
            others = [(p, n) for p, n in known_projects(conn) if p != project]
            if others:
                L.append("")
                L.append("Sessions have been collected for:")
                for p, n in others[:10]:
                    L.append(f"  {sanitize(p)}  ({n} session{'s' if n != 1 else ''})")
        else:
            L.append("No sessions collected yet. Start `runpeek watch` in a project and use your coding agent"
                     " normally.")
        return "\n".join(L)

    ids = short_ids([str(r["session_id"]) for r in shown])
    by_id = {str(r["session_id"]): r for r in shown}
    parents = [r for r in shown if not r["is_subagent"]]
    orphans = [r for r in shown if r["is_subagent"] and r["parent_session_id"] not in by_id]
    children: dict[str, list[sqlite3.Row]] = {}
    for r in shown:
        if r["is_subagent"] and r["parent_session_id"] in by_id:
            children.setdefault(str(r["parent_session_id"]), []).append(r)

    if detailed:
        L.append(f"{'Started':<18}{'Session':<14}{'Agent':<8}{'Tool calls':>11}{'Errors':>8}"
                 f"{'Model calls':>12}{'Input':>9}{'Cache r':>9}{'Cache w':>9}{'Output':>9}{'Est. cost':>11}"
                 f"{'Review':>8}  Work item")
    else:
        L.append(f"{'Started':<18}{'Session':<14}{'Agent':<8}{'Tool calls':>11}{'Errors':>8}"
                 f"{'Model calls':>12}{'Review':>8}  Work item" + ("  State" if include_empty else ""))

    def line(r: sqlite3.Row, indent: str = "") -> str:
        sid = ids[str(r["session_id"])]
        started = (indent + ui.when(_started(r), now)).ljust(18)
        agent = agent_label(r["source"])[:7]
        base = f"{started}{sid:<14}{agent:<8}{r['actions']:>11}{r['errors']:>8}{r['requests']:>12}"
        wi = assigned.get(str(r["session_id"]))
        work = "—" if wi is None else (wi[0] if wi[1] == "explicit" else f"{wi[0]} (via parent)")
        if detailed:
            cost = ui.usd_cents(r["equiv"]) + ("+?" if r["unpriced"] else "") if r["requests"] else "—"
            base += (f"{_k(r['inp']):>9}{_k(r['cr']):>9}{_k(r['cw']):>9}{_k(r['outp']):>9}{cost:>11}"
                     f"{r['findings']:>8}  {work}")
        else:
            base += f"{r['findings']:>8}  {work}"
            if include_empty:
                st = _state(r)
                base += f"  {st}" if st else ""
        return base

    for r in parents:
        L.append(line(r))
        for c in children.get(str(r["session_id"]), []):
            L.append(line(c, indent="  └ "))
    for r in orphans:
        L.append(line(r) + f"  (subagent of {sanitize(str(r['parent_session_id']))[:8]})")
    L.append("")
    L.append("Review = potential inefficiencies to look at. Work item: set with runpeek work assign <work-item>"
             " <session-id>")
    if detailed:
        L.append("Tokens are as reported in agent session records. Est. cost: " + COST_NOTE)
        L.append("'+?' = some model calls could not be priced (model not in the rate card).")
    L.append("")
    first = parents[0] if parents else shown[0]
    L.append("Review a session:")
    L.append(f"  runpeek session {ids[str(first['session_id'])]}")
    if not detailed:
        L.append("Usage and estimated cost per session: runpeek sessions --detailed")
    if not unassigned and any(str(r["session_id"]) not in assigned for r in shown):
        L.append("Sessions not on a work item: runpeek sessions --unassigned")
    return "\n".join(L)


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ------------------------------------------------------------------------ session


def render_session(conn: sqlite3.Connection, session_id: str, *, findings_only: bool = False,
                   term: Term | None = None, now: datetime | None = None) -> str:
    term = term or Term(color=False)
    s = conn.execute("SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if s is None:
        return f"No session {sanitize(session_id)!r}."
    row = next((r for r in _session_rows(conn, None, None) if r["session_id"] == session_id), None)
    assert row is not None
    short = session_id.split("/")[-1][:8]
    L: list[str] = []
    L.append(term.bold(f"SESSION {short}") + f"  ·  {ui.project_name(s['project_path'])} · started "
             f"{ui.when(_started(row), now)}" + ("  ·  subagent of " + sanitize(str(s["parent_session_id"]))[:8]
                                                 if s["is_subagent"] else ""))
    L.append(f"Project: {sanitize(s['project_path'] or '(unknown)')}  ·  Agent: {agent_label(s['source'])}"
             + (f"  ·  branch {sanitize(s['git_branch'])}" if s["git_branch"] else ""))
    wi, how = assignment_map(conn).get(session_id, (None, "none"))
    if wi:
        w = conn.execute("SELECT name FROM work_items WHERE work_item_id = ?", (wi,)).fetchone()
        L.append(f"Work item: {wi} {sanitize(w['name']) if w else ''}"
                 + (" (via parent session)" if how == "via_parent" else " (assigned explicitly)"))
    else:
        L.append(f"Work item: none — runpeek work assign <work-item> {short}")
    if s["usage_duplicates"]:
        L.append(term.amber(f"{s['usage_duplicates']} model calls in this transcript were already counted under "
                            f"session {str(s['duplicate_of_session_id'])[:8]} (resumed/forked copy); counted once."))
    if s["customer_id"] or s["job_name"]:
        L.append(f"Label: customer {sanitize(s['customer_id'] or '-')} · job {sanitize(s['job_name'] or '-')}"
                 " (set by you)")
    st = _state(row)
    if st:
        L.append(term.amber(f"State: {st}"))
    if s["format_status"] != "supported":
        L.append(term.amber("Parsed best-effort; figures may be incomplete."))
    L.append("")

    if not findings_only:
        L.append(term.bold("ACTIVITY"))
        L.append(f"  {row['turns']} turns · {row['actions']} tool calls · {row['errors']} failed tool calls"
                 f" · {row['requests']} model calls")
        tools = conn.execute(
            "SELECT tool_name, COUNT(*) n, SUM(CASE WHEN is_error=1 THEN 1 ELSE 0 END) e FROM agent_actions"
            " WHERE session_id = ? GROUP BY tool_name ORDER BY n DESC LIMIT 8",
            (session_id,),
        ).fetchall()
        if tools:
            L.append("  Most used: " + ", ".join(
                f"{sanitize(t['tool_name'])} {t['n']}" + (f" ({t['e']} failed)" if t["e"] else "") for t in tools))
        turns = conn.execute(
            "SELECT t.turn_id, t.started_at, t.duration_ms, t.assistant_messages, t.tool_calls,"
            " (SELECT COUNT(*) FROM agent_actions a WHERE a.turn_id = t.turn_id AND a.is_error = 1) errs"
            " FROM agent_turns t WHERE t.session_id = ? ORDER BY t.started_at",
            (session_id,),
        ).fetchall()
        if turns:
            L.append("")
            L.append(f"  {'Turn':<6}{'Started':<9}{'Duration':>12}{'Tool calls':>12}{'Failed':>8}{'Model calls':>13}")
            for i, t in enumerate(turns[-15:], start=max(1, len(turns) - 14)):
                dur = ui.duration(t["duration_ms"] / 1000) if t["duration_ms"] else "—"
                L.append(f"  {i:<6}{ui.clock(t['started_at']):<9}{dur:>12}{t['tool_calls']:>12}{t['errs']:>8}"
                         f"{t['assistant_messages']:>13}")
            if len(turns) > 15:
                L.append(f"  … {len(turns) - 15} earlier turns not shown")
            if any(t["duration_ms"] is None for t in turns):
                L.append("  Duration '—': no end-of-turn record in the transcript (not inferred from silence).")
        L.append("")

    fs = conn.execute("SELECT * FROM agent_findings WHERE session_id = ? ORDER BY first_at", (session_id,)).fetchall()
    L.append(term.bold(f"POTENTIAL INEFFICIENCIES ({len(fs)})"))
    if fs:
        kinds: dict[str, int] = {}
        for f in fs:
            k = title_for(f["kind"], json.loads(f["counts"]))
            kinds[k] = kinds.get(k, 0) + 1
        L.append("  " + " · ".join(f"{k.lower()} {n}" for k, n in kinds.items())
                 + " — each tool call is counted in at most one item")
        for f in fs:
            L.extend(render_finding(f, term, show_session=False))
    else:
        L.append("  None detected by the three current checks (repeated failure, repeated read, retry loop).")
    L.append("")

    if not findings_only:
        u = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(cache_read_tokens),0) cr,"
            " COALESCE(SUM(cache_write_5m_tokens),0) cw5, COALESCE(SUM(cache_write_1h_tokens),0) cw1,"
            " COALESCE(SUM(output_tokens),0) o,"
            " SUM(CASE WHEN api_equiv_status='priced' THEN api_equiv_nanos ELSE 0 END) equiv,"
            " SUM(CASE WHEN api_equiv_status!='priced' THEN 1 ELSE 0 END) unpriced FROM agent_usage"
            " WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        L.append(term.bold("USAGE AND ESTIMATED COST"))
        if u["n"]:
            L.append(f"  {u['n']} model calls · input {u['i']:,} · cache read {u['cr']:,}"
                     f" · cache write {u['cw5'] + u['cw1']:,} · output {u['o']:,} tokens")
            L.append(f"  {ui.usd_exact(u['equiv'] or 0)}  API-equivalent estimate"
                     + (f"  ({u['unpriced']} of {u['n']} calls not priced — total is incomplete)" if u["unpriced"]
                        else f"  (all {u['n']} calls priced)"))
            models = conn.execute(
                "SELECT model, COUNT(*) n, SUM(CASE WHEN api_equiv_status='priced' THEN 1 ELSE 0 END) p,"
                " SUM(CASE WHEN api_equiv_status='priced' THEN api_equiv_nanos ELSE 0 END) c,"
                " MIN(rate_resolution) res FROM agent_usage WHERE session_id = ? GROUP BY model ORDER BY n DESC",
                (session_id,),
            ).fetchall()
            for m in models:
                note = "" if m["p"] == m["n"] else f"  ({m['n'] - m['p']} not priced: model not in rate card)"
                fb = "  (fallback rate card)" if m["res"] and str(m["res"]).startswith("fallback") else ""
                L.append(f"    {sanitize(m['model'] or '(unknown model)'):<30}{m['n']:>5} calls"
                         f"  {ui.usd_exact(m['c']):>12}{note}{fb}")
            L.append(f"  Source-reported cost: not available in {records_label(s['source'])}.")
            for ln in term.wrap(cost_note(s["source"]), indent=2):
                L.append(ln)
        else:
            L.append("  No model calls with usage were recorded for this session.")
        L.append("")
    L.append(term.dim("Machine-readable detail: runpeek export --out sessions.jsonl"))
    return "\n".join(L)


# ----------------------------------------------------------------------- findings


def render_finding(f: sqlite3.Row, term: Term | None = None, *, show_session: bool = True) -> list[str]:
    term = term or Term(color=False)
    ev = json.loads(f["evidence"])
    counts = json.loads(f["counts"])
    title = title_for(f["kind"], counts)
    sid = str(f["session_id"]).split("/")[-1][:8]
    L: list[str] = [""]
    head = f"  {term.amber(title)}"
    if show_session:
        head += f"  ·  session {sid}"
    if f["updated_at"]:
        head += "  (updated)"
    L.append(head)
    for ln in term.wrap(sanitize(f["summary"]), indent=4):
        L.append(ln)
    window = f"{ui.clock(f['first_at'])}–{ui.clock(f['last_at'])}"
    usage = counts.get("usage_in_window")
    obs = [f"{len(ev)} tool calls", f"window {window}"]
    if usage and usage.get("available"):
        obs.append(f"{usage['requests']} model calls in the window")
    for ln in term.wrap("Observed: " + " · ".join(obs), indent=4):
        L.append(ln)
    evidence = "Evidence: " + "; ".join(
        f"{e['action_id'][-8:]} {ui.clock(e['at'])}{' failed' if e.get('error') else ''}" for e in ev[:6]
    ) + (f"; … {len(ev) - 6} more" if len(ev) > 6 else "")
    for ln in term.wrap(evidence, indent=4):
        L.append(ln)
    for ln in term.wrap("Next: " + sanitize(f["suggestion"]), indent=4):
        L.append(ln)
    for ln in term.wrap("Limits: " + sanitize(f["limitations"]), indent=4):
        L.append(ln)
    L.append(f"    Detail: runpeek session {sid}")
    return L


def render_findings(conn: sqlite3.Connection, project: str | None, last: int = 50, *, term: Term | None = None) -> str:
    term = term or Term(color=False)
    where = "" if project is None else " WHERE s.project_path = ?"
    params: tuple[Any, ...] = () if project is None else (project,)
    rows = conn.execute(
        "SELECT f.* FROM agent_findings f JOIN agent_sessions s ON s.session_id = f.session_id"
        f"{where} ORDER BY f.last_at DESC LIMIT ?", (*params, last),
    ).fetchall()
    L = [term.bold("POTENTIAL INEFFICIENCIES TO REVIEW")]
    L.append(f"Project: {ui.project_name(project) + ' (' + sanitize(project) + ')' if project else 'all projects'}"
             f" · newest first · {len(rows)} shown")
    if not rows:
        L.append("")
        L.append("None recorded" + (f" for {sanitize(project)}." if project else ".")
                 + (" Try: runpeek findings --all-projects" if project else ""))
        return "\n".join(L)
    for f in rows:
        L.extend(render_finding(f, term))
    L.append("")
    L.append("Each item is a potential inefficiency, not proven waste. Tool calls are counted in at most one item.")
    return "\n".join(L)
