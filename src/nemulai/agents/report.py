"""Session listings and detail. All figures come from stored metadata; the
API-equivalent estimate is labelled every time it is shown."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..money import format_usd

API_EQUIV_LABEL = "API-equivalent estimate at list rates — not a subscription charge, quota, or saving"


def _session_rows(conn: sqlite3.Connection, project: str | None, last: int) -> list[sqlite3.Row]:
    where = "" if project is None else " WHERE s.project_path = ?"
    params: tuple[Any, ...] = () if project is None else (project,)
    return conn.execute(
        "SELECT s.*,"
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
        f" FROM agent_sessions s{where} ORDER BY COALESCE(s.last_event_at, s.first_seen_at) DESC LIMIT ?",
        (*params, last),
    ).fetchall()


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _usd2(nanos: int) -> str:
    """Two-decimal display for the table; the session view keeps full precision."""
    return f"≈${nanos / 1e9:,.2f}"


def render_sessions(conn: sqlite3.Connection, project: str | None, last: int = 20) -> str:
    rows = _session_rows(conn, project, last)
    L: list[str] = []
    scope = f"project {project}" if project else "all projects"
    L.append(f"nemulai sessions · {scope} · {len(rows)} shown")
    L.append(f"tokens are provider-reported per request; $ is an {API_EQUIV_LABEL}")
    L.append("")
    L.append(f"{'session':<10}{'kind':<6}{'last activity':<21}{'turns':>6}{'actions':>8}{'errs':>6}"
             f"{'reqs':>6}{'in':>8}{'cache r':>9}{'cache w':>9}{'out':>8}{'api-equiv':>13}{'findings':>10}  status")
    for r in rows:
        sid = r["session_id"]
        short = sid.split("/")[-1][:8]
        kind = "sub" if r["is_subagent"] else "main"
        equiv = _usd2(r["equiv"]) if r["requests"] and r["unpriced"] == 0 else (
            f"{_usd2(r['equiv'])}+?" if r["equiv"] else "—")
        status = r["format_status"] if r["format_status"] != "supported" else ""
        L.append(f"{short:<10}{kind:<6}{(r['last_event_at'] or '')[:19]:<21}{r['turns']:>6}{r['actions']:>8}"
                 f"{r['errors']:>6}{r['requests']:>6}{_k(r['inp']):>8}{_k(r['cr']):>9}{_k(r['cw']):>9}{_k(r['outp']):>8}"
                 f"{equiv:>13}{r['findings']:>10}  {status}")
    if not rows:
        L.append("  (no sessions ingested — run `nemulai watch` in this project, or pass --all-projects)")
    L.append("")
    L.append("≈$ is rounded to cents for the table (`nemulai session <id>` shows full precision).")
    L.append("'+?' = some requests could not be priced (model not in the rate card). Sessions are attributed to a")
    L.append("project, never to a customer, unless you map them explicitly with `nemulai session <id> --set-customer`.")
    return "\n".join(L)


def resolve_session_id(conn: sqlite3.Connection, prefix: str) -> str | None:
    rows = conn.execute(
        "SELECT session_id FROM agent_sessions WHERE session_id = ? OR session_id LIKE ? OR session_id LIKE ?",
        (prefix, f"{prefix}%", f"%/{prefix}%"),
    ).fetchall()
    if len(rows) == 1:
        return str(rows[0]["session_id"])
    return None if not rows else (str(rows[0]["session_id"]) if any(r["session_id"] == prefix for r in rows) else None)


def render_session(conn: sqlite3.Connection, session_id: str, *, findings_only: bool = False) -> str:
    s = conn.execute("SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if s is None:
        return f"no session {session_id!r}"
    L: list[str] = []
    L.append(f"session {session_id}  ·  {s['source']} {s['source_version'] or ''}  ·  format {s['format_status']}")
    L.append(f"project {s['project_path'] or '?'}  ·  transcript {s['transcript_path']}")
    L.append(f"first event {(s['first_event_at'] or '?')[:19]}  ·  last event {(s['last_event_at'] or '?')[:19]}"
             f"  ·  entries {s['entries_ingested']} (unparseable {s['entries_unparseable']})")
    if s["customer_id"] or s["job_name"]:
        L.append(f"mapped to customer {s['customer_id'] or '-'} / job {s['job_name'] or '-'} (explicit)")
    if s["format_status"] != "supported":
        L.append(f"WARNING: transcript format {s['format_status']} — parsed best-effort; figures may be incomplete")
    L.append("")
    if not findings_only:
        u = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(cache_read_tokens),0) cr,"
            " COALESCE(SUM(cache_write_5m_tokens),0) cw5, COALESCE(SUM(cache_write_1h_tokens),0) cw1,"
            " COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(web_search_requests),0) ws,"
            " SUM(CASE WHEN api_equiv_status='priced' THEN api_equiv_nanos ELSE 0 END) equiv,"
            " SUM(CASE WHEN api_equiv_status!='priced' THEN 1 ELSE 0 END) unpriced,"
            " COUNT(DISTINCT model) models FROM agent_usage WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        L.append("USAGE (provider-reported, per API request, deduplicated by message id)")
        L.append(f"  requests {u['n']}  ·  input {u['i']:,}  ·  cache read {u['cr']:,}  ·  cache write 5m {u['cw5']:,}"
                 f" / 1h {u['cw1']:,}  ·  output {u['o']:,}  ·  web searches {u['ws']}")
        L.append("  source-reported cost: not available from Claude Code transcripts"
                 " (only via its OpenTelemetry export)")
        eq = format_usd(u["equiv"] or 0)
        L.append(f"  {API_EQUIV_LABEL}: {eq}" + (f"  (+{u['unpriced']} requests unpriced)" if u["unpriced"] else ""))
        models = conn.execute(
            "SELECT model, COUNT(*) n, SUM(CASE WHEN api_equiv_status='priced' THEN 1 ELSE 0 END) p,"
            " MIN(rate_card_id) card, MIN(rate_resolution) res FROM agent_usage WHERE session_id = ? GROUP BY model",
            (session_id,),
        ).fetchall()
        for m in models:
            L.append(f"    {m['model'] or '(unknown model)':<32} {m['n']:>5} requests  priced {m['p']}/{m['n']}"
                     + (f"  card {m['card']} ({m['res']})" if m["card"] else "  no rate card"))
        L.append("")
        turns = conn.execute(
            "SELECT t.turn_id, t.started_at, t.duration_ms, t.assistant_messages, t.tool_calls,"
            " (SELECT COUNT(*) FROM agent_actions a WHERE a.turn_id = t.turn_id AND a.is_error = 1) errs,"
            " (SELECT COALESCE(SUM(output_tokens),0) FROM agent_usage u WHERE u.turn_id = t.turn_id) outp"
            " FROM agent_turns t WHERE t.session_id = ? ORDER BY t.started_at",
            (session_id,),
        ).fetchall()
        L.append(f"TURNS ({len(turns)})     started              duration   requests  actions  errors  output tok")
        for t in turns[-25:]:
            dur = f"{t['duration_ms'] / 1000:.0f}s" if t["duration_ms"] else "—"
            L.append(f"  {t['turn_id'][:12]:<12} {(t['started_at'] or '')[:19]:<20} {dur:>8}"
                     f"   {t['assistant_messages']:>8}  {t['tool_calls']:>7}  {t['errs']:>6}  {t['outp']:>10,}")
        if len(turns) > 25:
            L.append(f"  … {len(turns) - 25} earlier turns not shown")
        L.append("")
        tools = conn.execute(
            "SELECT tool_name, COUNT(*) n, SUM(CASE WHEN is_error=1 THEN 1 ELSE 0 END) e,"
            " AVG(duration_ms) d FROM agent_actions WHERE session_id = ? GROUP BY tool_name ORDER BY n DESC",
            (session_id,),
        ).fetchall()
        L.append("ACTIONS by tool          calls  errors  avg ms")
        for t in tools:
            L.append(f"  {t['tool_name'][:22]:<22} {t['n']:>6}  {t['e']:>6}  {(t['d'] or 0):>6.0f}")
        L.append("")
    fs = conn.execute(
        "SELECT * FROM agent_findings WHERE session_id = ? ORDER BY first_at", (session_id,)
    ).fetchall()
    L.append(f"FINDINGS ({len(fs)}) — each is a potential inefficiency, not a verdict")
    for f in fs:
        L.extend(render_finding(f))
    if not fs:
        L.append("  none detected by the three current diagnostics"
                 " (repeated failing action, repeated read, retry loop)")
    return "\n".join(L)


def render_finding(f: sqlite3.Row) -> list[str]:
    ev = json.loads(f["evidence"])
    counts = json.loads(f["counts"])
    L = [""]
    L.append(f"  [{f['kind']}] {f['summary']}")
    L.append(f"    session {f['session_id'][:8]}  turn {(f['turn_id'] or '-')[:12]}"
             f"  window {(f['first_at'] or '')[11:19]} → {(f['last_at'] or '')[11:19]}")
    usage = counts.pop("usage_in_window", None)
    L.append("    observed: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v is not None))
    if usage and usage.get("available"):
        L.append(f"    usage in window: {usage['requests']} requests, input {usage['input_tokens']:,},"
                 f" cache read {usage['cache_read_tokens']:,}, output {usage['output_tokens']:,}")
    L.append("    evidence: " + "; ".join(
        f"{e['action_id'][-8:]} @{(e['at'] or '')[11:19]}{' ✗' if e.get('error') else ''}" for e in ev[:8])
             + (f"; … {len(ev) - 8} more" if len(ev) > 8 else ""))
    L.append(f"    limits: {f['limitations']}")
    L.append(f"    try: {f['suggestion']}")
    return L
