"""Work items: the unit of accounting across sessions, agents, models, retries
and subagents.

Rules that keep the numbers trustworthy:

* A session is assigned to at most one work item (primary key on session_id).
  A subagent session follows its parent unless it is explicitly assigned
  elsewhere. No usage record can therefore be counted under two items.
* Assignment is explicit. Repository, branch and issue data only *suggest*
  candidates; nothing is written without a user command.
* Every number in a report is built from ``agent_usage`` rows that carry
  their own rate card id, resolution and calc version, so a total can be
  traced back to the source records that produced it.
* The total is an API-equivalent estimate of *model usage only*. It is never
  the cost of a deployment, an infrastructure bill, or what a subscription
  charged.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import ui
from ..ids import now_iso
from ..money import tokens_cost_nanos
from ..rates import RateCard, RateCardSet
from ..ui import Term, sanitize
from .ingest import ADAPTERS

KINDS = ("task", "feature", "bugfix", "deployment")
STATUSES = ("open", "closed")
OUTCOMES = ("completed", "incomplete", "failed", "abandoned")

COST_LABEL = "estimated model cost (API-equivalent, list prices)"
SCOPE_NOTE = ("Model usage only, as reported in agent session records: not infrastructure, CI, hosting or "
              "provider billing. Not a subscription charge. Estimates use dated list-price rate cards.")


class WorkItemError(Exception):
    pass


# --------------------------------------------------------------------------- CRUD


def new_work_item_id(conn: sqlite3.Connection) -> str:
    while True:
        wid = "wi-" + secrets.token_hex(3)
        if conn.execute("SELECT 1 FROM work_items WHERE work_item_id = ?", (wid,)).fetchone() is None:
            return wid


def create(conn: sqlite3.Connection, name: str, kind: str, *, repository: str | None = None,
           issue: str | None = None, branch: str | None = None, pr: str | None = None,
           deployment: str | None = None, note: str | None = None) -> str:
    name = name.strip()
    if not name:
        raise WorkItemError("a work item needs a name")
    if kind not in KINDS:
        raise WorkItemError(f"kind must be one of {', '.join(KINDS)}")
    wid = new_work_item_id(conn)
    now = now_iso()
    conn.execute(
        "INSERT INTO work_items (work_item_id, name, kind, repository, status, outcome, issue_ref, branch, pr_ref,"
        " deployment_ref, note, created_at, updated_at) VALUES (?,?,?,?,'open',NULL,?,?,?,?,?,?,?)",
        (wid, name, kind, repository, issue, branch, pr, deployment, note, now, now),
    )
    _event(conn, wid, "created", {"name": name, "kind": kind, "repository": repository, "issue": issue,
                                  "branch": branch, "pr": pr, "deployment": deployment})
    conn.commit()
    return wid


def edit(conn: sqlite3.Connection, wid: str, **fields: str | None) -> None:
    allowed = {"name": "name", "repository": "repository", "issue": "issue_ref", "branch": "branch",
               "pr": "pr_ref", "deployment": "deployment_ref", "note": "note", "kind": "kind"}
    sets, params, detail = [], [], {}
    for k, v in fields.items():
        if v is None:
            continue
        if k not in allowed:
            raise WorkItemError(f"unknown field {k!r}")
        if k == "kind" and v not in KINDS:
            raise WorkItemError(f"kind must be one of {', '.join(KINDS)}")
        sets.append(f"{allowed[k]} = ?")
        params.append(v)
        detail[k] = v
    if not sets:
        return
    params.extend([now_iso(), wid])
    conn.execute(f"UPDATE work_items SET {', '.join(sets)}, updated_at = ? WHERE work_item_id = ?", params)
    _event(conn, wid, "edited", detail)
    conn.commit()


def set_status(conn: sqlite3.Connection, wid: str, *, status: str | None = None, outcome: str | None = None) -> None:
    row = get(conn, wid)
    if row is None:
        raise WorkItemError(f"no work item {wid!r}")
    if status is not None and status not in STATUSES:
        raise WorkItemError(f"status must be one of {', '.join(STATUSES)}")
    if outcome is not None and outcome not in OUTCOMES:
        raise WorkItemError(f"outcome must be one of {', '.join(OUTCOMES)}")
    new_status = status or row["status"]
    new_outcome = outcome if outcome is not None else row["outcome"]
    if new_status == "closed" and new_outcome is None:
        raise WorkItemError("closing a work item needs an outcome: completed | incomplete | failed | abandoned")
    if new_status == "open" and status == "open":
        new_outcome = outcome  # reopening clears the outcome unless one is given
    now = now_iso()
    conn.execute(
        "UPDATE work_items SET status = ?, outcome = ?, updated_at = ?,"
        " closed_at = CASE WHEN ? = 'closed' THEN COALESCE(closed_at, ?) ELSE NULL END WHERE work_item_id = ?",
        (new_status, new_outcome, now, new_status, now, wid),
    )
    _event(conn, wid, "status", {"status": new_status, "outcome": new_outcome})
    conn.commit()


def get(conn: sqlite3.Connection, wid: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute("SELECT * FROM work_items WHERE work_item_id = ?", (wid,)).fetchone()
    return row


def resolve(conn: sqlite3.Connection, ref: str) -> str | list[str]:
    """Exact id, exact name, or unique id prefix. Otherwise the candidates."""
    ref = ref.strip()
    if get(conn, ref) is not None:
        return ref
    by_name = [str(r[0]) for r in conn.execute("SELECT work_item_id FROM work_items WHERE name = ?", (ref,))]
    if len(by_name) == 1:
        return by_name[0]
    pref = ref if ref.startswith("wi-") else f"wi-{ref}"
    rows = [str(r[0]) for r in conn.execute(
        "SELECT work_item_id FROM work_items WHERE work_item_id LIKE ? ORDER BY work_item_id", (pref + "%",))]
    if len(rows) == 1:
        return rows[0]
    return rows or by_name


def list_items(conn: sqlite3.Connection, *, include_closed: bool = True) -> list[sqlite3.Row]:
    where = "" if include_closed else " WHERE status = 'open'"
    return conn.execute(f"SELECT * FROM work_items{where} ORDER BY created_at DESC").fetchall()


def _event(conn: sqlite3.Connection, wid: str, kind: str, detail: dict[str, Any]) -> None:
    conn.execute("INSERT INTO work_item_events (work_item_id, at, kind, detail) VALUES (?,?,?,?)",
                 (wid, now_iso(), kind, json.dumps(detail, sort_keys=True)))


# --------------------------------------------------------------------------- assignment


def assign(conn: sqlite3.Connection, wid: str, session_ids: list[str]) -> list[tuple[str, str | None]]:
    """Assign sessions explicitly. Returns (session_id, previous work item or None) per session.
    A session already on this item is a no-op; one on another item is reassigned (recorded)."""
    if get(conn, wid) is None:
        raise WorkItemError(f"no work item {wid!r}")
    out: list[tuple[str, str | None]] = []
    now = now_iso()
    for sid in session_ids:
        if conn.execute("SELECT 1 FROM agent_sessions WHERE session_id = ?", (sid,)).fetchone() is None:
            raise WorkItemError(f"no session {sid!r}")
        prev = conn.execute("SELECT work_item_id FROM work_item_sessions WHERE session_id = ?", (sid,)).fetchone()
        prev_id = str(prev["work_item_id"]) if prev else None
        if prev_id == wid:
            out.append((sid, prev_id))
            continue
        conn.execute(
            "INSERT INTO work_item_sessions (session_id, work_item_id, assigned_at, assigned_by) VALUES (?,?,?,'user')"
            " ON CONFLICT(session_id) DO UPDATE SET work_item_id = excluded.work_item_id,"
            " assigned_at = excluded.assigned_at, assigned_by = 'user'", (sid, wid, now))
        _event(conn, wid, "reassigned" if prev_id else "assigned", {"session_id": sid, "from": prev_id})
        if prev_id:
            _event(conn, prev_id, "unassigned", {"session_id": sid, "to": wid})
        out.append((sid, prev_id))
    conn.execute("UPDATE work_items SET updated_at = ? WHERE work_item_id = ?", (now, wid))
    conn.commit()
    return out


def unassign(conn: sqlite3.Connection, session_ids: list[str]) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for sid in session_ids:
        prev = conn.execute("SELECT work_item_id FROM work_item_sessions WHERE session_id = ?", (sid,)).fetchone()
        if prev:
            conn.execute("DELETE FROM work_item_sessions WHERE session_id = ?", (sid,))
            _event(conn, str(prev["work_item_id"]), "unassigned", {"session_id": sid, "to": None})
        out.append((sid, str(prev["work_item_id"]) if prev else None))
    conn.commit()
    return out


def effective_assignment(conn: sqlite3.Connection, session_id: str) -> tuple[str | None, str]:
    """Resolve the nearest explicit ancestor, stopping safely on malformed cycles."""
    seen: set[str] = set()
    current = session_id
    while current not in seen:
        seen.add(current)
        row = conn.execute("SELECT work_item_id FROM work_item_sessions WHERE session_id = ?",
                           (current,)).fetchone()
        if row:
            return str(row["work_item_id"]), "explicit" if current == session_id else "via_parent"
        parent = conn.execute("SELECT parent_session_id FROM agent_sessions WHERE session_id = ?",
                              (current,)).fetchone()
        if parent is None or not parent["parent_session_id"]:
            break
        current = str(parent["parent_session_id"])
    return None, "none"


def effective_sessions(conn: sqlite3.Connection, wid: str) -> list[tuple[sqlite3.Row, str]]:
    """Use the same ancestry resolution for reports and assignment views."""
    out: list[tuple[sqlite3.Row, str]] = []
    for session in conn.execute(
        "SELECT * FROM agent_sessions ORDER BY COALESCE(first_event_at, first_seen_at)"
    ).fetchall():
        owner, how = effective_assignment(conn, str(session["session_id"]))
        if owner == wid:
            out.append((session, how))
    return out


def assignment_map(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """session_id → (work_item_id, how), independent of insertion order."""
    out: dict[str, tuple[str, str]] = {}
    for session in conn.execute("SELECT session_id FROM agent_sessions").fetchall():
        sid = str(session["session_id"])
        owner, how = effective_assignment(conn, sid)
        if owner is not None:
            out[sid] = (owner, how)
    return out


def suggest(conn: sqlite3.Connection, wid: str, *, limit: int = 20) -> list[tuple[sqlite3.Row, list[str]]]:
    """Unassigned sessions that share the work item's repository, branch or issue text.
    Ranked by the number of matching hints; nothing is assigned."""
    item = get(conn, wid)
    if item is None:
        raise WorkItemError(f"no work item {wid!r}")
    assigned = assignment_map(conn)
    rows = conn.execute(
        "SELECT * FROM agent_sessions WHERE parent_session_id IS NULL"
        " ORDER BY COALESCE(first_event_at, first_seen_at) DESC").fetchall()
    out: list[tuple[sqlite3.Row, list[str]]] = []
    repo = (item["repository"] or "").rstrip("/")
    branch = item["branch"]
    for r in rows:
        if str(r["session_id"]) in assigned:
            continue
        why: list[str] = []
        if repo and r["project_path"] and str(r["project_path"]).rstrip("/") == repo:
            why.append("same project path")
        if repo and r["repository_url"] and str(r["repository_url"]).rstrip("/") == repo:
            why.append("same repository url")
        if branch and r["git_branch"] and r["git_branch"] == branch:
            why.append(f"branch {branch}")
        if why:
            out.append((r, why))
    out.sort(key=lambda t: (-len(t[1]), str(t[0]["first_event_at"] or "")))
    return out[:limit]


# --------------------------------------------------------------------------- report


@dataclass
class Bucket:
    calls: int = 0
    priced_calls: int = 0
    unpriced_calls: int = 0
    no_usage_calls: int = 0
    cost_nanos: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, u: sqlite3.Row, amount: int | None, status: str) -> None:
        self.calls += 1
        if status == "priced" and amount is not None:
            self.priced_calls += 1
            self.cost_nanos += amount
        elif status == "no_usage":
            self.no_usage_calls += 1
        else:
            self.unpriced_calls += 1
        self.input_tokens += u["input_tokens"] or 0
        self.cache_read_tokens += u["cache_read_tokens"] or 0
        self.cache_write_tokens += (u["cache_write_5m_tokens"] or 0) + (u["cache_write_1h_tokens"] or 0)
        self.output_tokens += u["output_tokens"] or 0
        self.reasoning_tokens += u["reasoning_tokens"] or 0


@dataclass
class SessionLine:
    session_id: str
    source: str
    how: str
    started: str | None
    parent_session_id: str | None
    format_status: str
    bucket: Bucket = field(default_factory=Bucket)
    tool_calls: int = 0
    failed_tool_calls: int = 0
    findings: int = 0
    usage_duplicates: int = 0
    duplicate_of: str | None = None
    consistency: dict[str, int] = field(default_factory=dict)
    transcript_path: str = ""
    entries: int = 0
    unparseable: int = 0


@dataclass
class Report:
    item: dict[str, Any]
    sessions: list[SessionLine]
    total: Bucket
    by_agent: dict[str, Bucket]
    by_model: dict[tuple[str, str], Bucket]  # (source, model)
    timeline: list[tuple[str, Bucket]]  # (bucket label, usage)
    timeline_unit: str
    unpriced_reasons: dict[str, int]
    rate_cards: dict[tuple[str, str], int]  # (rate_card_id, resolution) → calls
    calc_versions: set[int]
    pinned: str | None
    subagent_count: int
    explicit_count: int
    first_at: str | None
    last_at: str | None

    @property
    def coverage(self) -> str:
        t = self.total
        if t.calls == 0:
            return "no model calls recorded"
        if t.priced_calls == t.calls:
            return "complete"
        return "partial"


def _pin_price(card: RateCard, u: sqlite3.Row) -> tuple[int | None, str]:
    """Re-price one usage row under a pinned card (on the fly; nothing stored)."""
    has = any(u[k] is not None for k in ("input_tokens", "output_tokens", "cache_read_tokens"))
    if not has:
        return None, "no_usage"
    _, prices = card.prices_for(u["model"])
    if prices is None:
        return None, "unpriced"
    amount = (tokens_cost_nanos(u["input_tokens"] or 0, prices.input)
              + tokens_cost_nanos(u["cache_write_5m_tokens"] or 0, prices.cache_write_5m or prices.input)
              + tokens_cost_nanos(u["cache_write_1h_tokens"] or 0, prices.cache_write_1h or prices.input)
              + tokens_cost_nanos(u["cache_read_tokens"] or 0, prices.cached_input)
              + tokens_cost_nanos(u["output_tokens"] or 0, prices.output))
    return amount, "priced"


def build_report(conn: sqlite3.Connection, wid: str, *, cards: RateCardSet | None = None,
                 pin: str | None = None) -> Report:
    item = get(conn, wid)
    if item is None:
        raise WorkItemError(f"no work item {wid!r}")
    pinned_card: RateCard | None = None
    if pin:
        cards = cards or RateCardSet.builtin()
        pinned_card = cards.get(pin)
        if pinned_card is None:
            raise WorkItemError(f"unknown rate card {pin!r}; known: {', '.join(cards.ids())}")
    sessions = effective_sessions(conn, wid)
    lines: list[SessionLine] = []
    total = Bucket()
    by_agent: dict[str, Bucket] = {}
    by_model: dict[tuple[str, str], Bucket] = {}
    per_time: dict[str, Bucket] = {}
    unpriced_reasons: dict[str, int] = {}
    rate_cards: dict[tuple[str, str], int] = {}
    calc_versions: set[int] = set()
    first_at: str | None = None
    last_at: str | None = None
    all_usage: list[tuple[sqlite3.Row, int | None, str, str]] = []
    for s, how in sessions:
        sid = str(s["session_id"])
        line = SessionLine(
            session_id=sid, source=str(s["source"]), how=how,
            started=str(s["first_event_at"] or s["first_seen_at"] or "") or None,
            parent_session_id=s["parent_session_id"], format_status=str(s["format_status"]),
            usage_duplicates=int(s["usage_duplicates"] or 0), duplicate_of=s["duplicate_of_session_id"],
            consistency=json.loads(s["usage_consistency"]) if s["usage_consistency"] else {},
            transcript_path=str(s["transcript_path"]), entries=int(s["entries_ingested"] or 0),
            unparseable=int(s["entries_unparseable"] or 0),
        )
        a = conn.execute("SELECT COUNT(*) n, SUM(CASE WHEN is_error=1 THEN 1 ELSE 0 END) e FROM agent_actions"
                         " WHERE session_id = ?", (sid,)).fetchone()
        line.tool_calls, line.failed_tool_calls = int(a["n"]), int(a["e"] or 0)
        line.findings = int(conn.execute("SELECT COUNT(*) FROM agent_findings WHERE session_id = ?",
                                         (sid,)).fetchone()[0])
        for u in conn.execute("SELECT * FROM agent_usage WHERE session_id = ? ORDER BY at", (sid,)):
            if pinned_card is not None and (u["provider"] or "") == pinned_card.provider:
                # a pin re-prices only the card's own provider; other providers keep their stored estimates
                amount, status = _pin_price(pinned_card, u)
                card_key = (pinned_card.rate_card_id, "pinned")
            else:
                amount, status = u["api_equiv_nanos"], str(u["api_equiv_status"])
                card_key = (str(u["rate_card_id"] or "(none)"), str(u["rate_resolution"] or "none"))
            all_usage.append((u, amount, status, line.source))
            line.bucket.add(u, amount, status)
            total.add(u, amount, status)
            by_agent.setdefault(line.source, Bucket()).add(u, amount, status)
            by_model.setdefault((line.source, str(u["model"] or "(model not reported)")), Bucket()).add(u, amount,
                                                                                                        status)
            if status != "priced":
                if status == "no_usage":
                    reason = "no usage reported"
                elif u["model"] is None:
                    reason = "model not reported in the record"
                elif pinned_card is not None and (u["provider"] or "") == pinned_card.provider:
                    reason = f"model {u['model']} not in {pinned_card.rate_card_id}"
                else:
                    reason = f"model {u['model']} not in {u['rate_card_id'] or 'any rate card'}"
                unpriced_reasons[reason] = unpriced_reasons.get(reason, 0) + 1
            if status == "priced":
                rate_cards[card_key] = rate_cards.get(card_key, 0) + 1
            if u["calc_version"] is not None:
                calc_versions.add(int(u["calc_version"]))
            if u["at"]:
                first_at = u["at"] if first_at is None or u["at"] < first_at else first_at
                last_at = u["at"] if last_at is None or u["at"] > last_at else last_at
        lines.append(line)
    # timeline: by day, or by hour when everything fits in 48 hours
    unit = "day"
    d0, d1 = ui.parse_ts(first_at), ui.parse_ts(last_at)
    if d0 and d1 and (d1 - d0).total_seconds() <= 48 * 3600:
        unit = "hour"
    for u, amount, status, _src in all_usage:
        d = ui.parse_ts(u["at"])
        if d is None:
            label = "(no timestamp)"
        else:
            loc = d.astimezone()
            label = f"{loc:%Y-%m-%d %H:00}" if unit == "hour" else f"{loc:%Y-%m-%d}"
        per_time.setdefault(label, Bucket()).add(u, amount, status)
    timeline = sorted(per_time.items())
    return Report(
        item=dict(item), sessions=lines, total=total, by_agent=by_agent, by_model=by_model, timeline=timeline,
        timeline_unit=unit, unpriced_reasons=unpriced_reasons, rate_cards=rate_cards, calc_versions=calc_versions,
        pinned=pin, subagent_count=sum(1 for ln in lines if ln.parent_session_id),
        explicit_count=sum(1 for ln in lines if ln.how == "explicit"), first_at=first_at, last_at=last_at,
    )


# --------------------------------------------------------------------------- rendering


def _label(source: str) -> str:
    a = ADAPTERS.get(source)
    return str(a.LABEL) if a else source


def outcome_line(item: dict[str, Any]) -> str:
    if item["status"] == "closed":
        return f"{item['outcome']} (closed {ui.when(item['closed_at'])})"
    return "in progress (open, no outcome recorded)"


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _cost_cell(b: Bucket) -> str:
    if b.calls == 0:
        return "—"
    s = ui.usd_exact(b.cost_nanos) if b.priced_calls else "—"
    if b.priced_calls < b.calls:
        s += "+?"
    return s


def render_report(r: Report, *, term: Term | None = None, trace: int = 0, conn: sqlite3.Connection | None = None,
                  now: datetime | None = None) -> str:
    term = term or Term(color=False)
    it = r.item
    t = r.total
    L: list[str] = []
    L.append(term.bold(f"WORK ITEM {it['work_item_id']}  ·  {sanitize(it['name'])}")
             + f"  ·  {it['kind']}")
    refs = [f"repository {sanitize(it['repository'])}" if it["repository"] else None,
            f"issue {sanitize(it['issue_ref'])}" if it["issue_ref"] else None,
            f"branch {sanitize(it['branch'])}" if it["branch"] else None,
            f"PR {sanitize(it['pr_ref'])}" if it["pr_ref"] else None,
            f"deployment {sanitize(it['deployment_ref'])}" if it["deployment_ref"] else None]
    refs_s = " · ".join(x for x in refs if x)
    if refs_s:
        L.append(refs_s)
    L.append(f"Outcome: {outcome_line(it)}")
    L.append(f"Created {ui.when(it['created_at'], now)}"
             + (f" · activity {ui.when(r.first_at, now)} → {ui.when(r.last_at, now)}" if r.first_at else
                " · no model usage recorded yet"))
    L.append("")

    # ---- headline
    L.append(term.bold("ESTIMATED MODEL COST"))
    if t.calls == 0:
        L.append("  No model calls recorded for the assigned sessions.")
    else:
        head = f"  {ui.usd_exact(t.cost_nanos)}  {COST_LABEL}"
        if r.pinned:
            head += f"  [pinned rate card {r.pinned} for its provider; stored estimates untouched]"
        L.append(head)
        if t.priced_calls == t.calls:
            L.append(f"  all {t.calls} model calls priced · coverage complete")
        else:
            L.append(f"  {t.priced_calls} of {t.calls} model calls priced · total is a priced subtotal, not the whole"
                     f" ({t.unpriced_calls} unpriced, {t.no_usage_calls} without usage)")
        L.append(f"  tokens: input {t.input_tokens:,} · cache read {t.cache_read_tokens:,}"
                 f" · cache write {t.cache_write_tokens:,} · output {t.output_tokens:,}"
                 + (f" (reasoning {t.reasoning_tokens:,})" if t.reasoning_tokens else ""))
    for ln in term.wrap(SCOPE_NOTE, indent=2):
        L.append(ln)
    L.append("")

    # ---- breakdowns
    if t.calls:
        L.append(term.bold("BY AGENT"))
        L.append("  * Share of priced subtotal only; unknown costs are excluded.")
        L.append(f"  {'Agent':<14}{'Sessions':>9}{'Calls':>8}{'Est. cost':>14}{'Share*':>8}  Unpriced")
        for src, b in sorted(r.by_agent.items(), key=lambda kv: -kv[1].cost_nanos):
            n_s = sum(1 for ln in r.sessions if ln.source == src)
            share = (f"{100 * b.cost_nanos / t.cost_nanos:.1f}%"
                     if t.cost_nanos and b.priced_calls else "unknown" if b.calls else "—")
            L.append(f"  {_label(src):<14}{n_s:>9}{b.calls:>8}{_cost_cell(b):>14}{share:>8}"
                     f"  {b.unpriced_calls + b.no_usage_calls or ''}")
        L.append("")
        L.append(term.bold("BY MODEL"))
        L.append(f"  {'Model':<34}{'Agent':<12}{'Calls':>7}{'Input':>9}{'Cache r':>9}{'Output':>9}{'Est. cost':>14}")
        for (src, model), b in sorted(r.by_model.items(), key=lambda kv: -kv[1].cost_nanos):
            L.append(f"  {sanitize(model)[:33]:<34}{_label(src)[:11]:<12}{b.calls:>7}{_k(b.input_tokens):>9}"
                     f"{_k(b.cache_read_tokens):>9}{_k(b.output_tokens):>9}{_cost_cell(b):>14}")
        L.append("")
        L.append(term.bold("BY SESSION") + f"  ({r.explicit_count} assigned"
                 + (f", {r.subagent_count} subagent{'s' if r.subagent_count != 1 else ''} included via parent"
                    if r.subagent_count else "") + ")")
        L.append(f"  {'Started':<18}{'Session':<14}{'Agent':<12}{'Calls':>7}{'Tools':>7}{'Failed':>7}"
                 f"{'Est. cost':>14}{'Share*':>8}  Notes")
        for ln_ in sorted(r.sessions, key=lambda x: -x.bucket.cost_nanos):
            b = ln_.bucket
            short = ln_.session_id.split("/")[-1][:12]
            share = (f"{100 * b.cost_nanos / t.cost_nanos:.1f}%"
                     if t.cost_nanos and b.priced_calls else "unknown" if b.calls else "—")
            notes: list[str] = []
            if ln_.how == "via_parent":
                notes.append(f"subagent of {str(ln_.parent_session_id).split('/')[-1][:8]}")
            if ln_.format_status != "supported":
                notes.append(ln_.format_status.replace("_", " "))
            if ln_.usage_duplicates:
                notes.append(f"{ln_.usage_duplicates} calls counted under {str(ln_.duplicate_of)[:8]}")
            if ln_.findings:
                notes.append(f"{ln_.findings} to review")
            L.append(f"  {ui.when(ln_.started, now):<18}{short:<14}{_label(ln_.source)[:11]:<12}{b.calls:>7}"
                     f"{ln_.tool_calls:>7}{ln_.failed_tool_calls:>7}{_cost_cell(b):>14}{share:>8}  "
                     + ", ".join(notes))
        L.append("")
        L.append(term.bold("TIMELINE") + f"  (by {r.timeline_unit}, local time)")
        width = max((b.cost_nanos for _, b in r.timeline), default=0)
        for label, b in r.timeline:
            bar = "█" * int(round(24 * b.cost_nanos / width)) if width and b.cost_nanos else ""
            L.append(f"  {label:<18}{b.calls:>6} calls {_cost_cell(b):>14}  {bar}")
        L.append("")

    # ---- coverage and trust
    L.append(term.bold("ACCOUNTING COVERAGE"))
    L.append(f"  Sessions: {len(r.sessions)} counted · {r.explicit_count} assigned explicitly"
             + (f" · {r.subagent_count} subagents via parent" if r.subagent_count else ""))
    if t.calls:
        L.append(f"  Model calls: {t.priced_calls} priced · {t.unpriced_calls} unpriced · {t.no_usage_calls} without"
                 f" usage → coverage {r.coverage}")
    for reason, n in sorted(r.unpriced_reasons.items(), key=lambda kv: -kv[1]):
        L.append(f"    {n} × {sanitize(reason)}")
    unk = [ln_ for ln_ in r.sessions if ln_.format_status != "supported"]
    if unk:
        L.append(f"  {len(unk)} session{'s' if len(unk) != 1 else ''} with an unsupported record format, parsed"
                 " best-effort: " + ", ".join(x.session_id.split("/")[-1][:8] for x in unk[:6]))
    dups = [ln_ for ln_ in r.sessions if ln_.usage_duplicates]
    for d in dups:
        L.append(f"  Session {d.session_id.split('/')[-1][:8]} replays {d.usage_duplicates} model calls already"
                 f" counted under {str(d.duplicate_of)[:8]} (resumed/forked); counted once.")
    stale = sum(ln_.consistency.get("stale_usage_repeats", 0) for ln_ in r.sessions)
    resets = sum(ln_.consistency.get("total_resets", 0) for ln_ in r.sessions)
    nomodel = sum(ln_.consistency.get("usage_without_model", 0) for ln_ in r.sessions)
    if stale:
        L.append(f"  {stale} repeated usage snapshots ignored (cumulative total unchanged; Codex).")
    if resets:
        L.append(f"  {resets} cumulative-counter resets handled from the per-call block (Codex).")
    if nomodel:
        L.append(f"  {nomodel} usage records carried no model name; they stay unpriced.")
    bad = sum(ln_.unparseable for ln_ in r.sessions)
    if bad:
        L.append(f"  {bad} unparseable record lines skipped.")
    L.append("  Not measured: sessions written by other tools or outside the watched locations; work done"
             " without an agent; infrastructure or billing.")
    L.append("")

    L.append(term.bold("PRICING PROVENANCE"))
    if r.rate_cards:
        for (card, res), n in sorted(r.rate_cards.items(), key=lambda kv: -kv[1]):
            L.append(f"  {n} calls priced with {card} ({res.replace('_', ' ')})")
    else:
        L.append("  No calls priced.")
    if r.calc_versions:
        L.append(f"  Calculation version {', '.join(str(v) for v in sorted(r.calc_versions))}."
                 " Source-reported cost: not available in agent records.")
    L.append("  Every estimate row carries its usage id, request id (when the source has one), rate card and"
             " calculation version: runpeek work show --trace / runpeek export.")
    L.append("")

    if trace and conn is not None:
        L.append(term.bold(f"SOURCE EVENTS (last {trace})"))
        L.append(f"  {'at':<20}{'session':<10}{'model':<26}{'in':>8}{'cache r':>9}{'out':>7}{'cost':>13}"
                 "  usage id / request id")
        sids = [ln_.session_id for ln_ in r.sessions]
        if sids:
            q = ",".join("?" for _ in sids)
            rows = conn.execute(f"SELECT * FROM agent_usage WHERE session_id IN ({q}) ORDER BY at DESC LIMIT ?",
                                (*sids, trace)).fetchall()
            for u in reversed(rows):
                priced = u["api_equiv_status"] == "priced"
                cost = ui.usd_exact(u["api_equiv_nanos"]) if priced else str(u["api_equiv_status"])
                L.append(f"  {str(u['at'] or '')[:19]:<20}{str(u['session_id']).split('/')[-1][:8]:<10}"
                         f"{sanitize(u['model'] or '(none)')[:25]:<26}{u['input_tokens'] or 0:>8}"
                         f"{u['cache_read_tokens'] or 0:>9}{u['output_tokens'] or 0:>7}{cost:>13}  "
                         f"{sanitize(u['usage_id'])[:40]}"
                         + (f" / {sanitize(u['request_id'])}" if u["request_id"] else ""))
        L.append("")

    L.append(term.dim("Sessions: runpeek session <id> · Assign more: runpeek work assign "
                      f"{it['work_item_id']} <session-id>… · Suggestions: runpeek work suggest {it['work_item_id']}"))
    return "\n".join(L)


def render_list(conn: sqlite3.Connection, *, term: Term | None = None, include_closed: bool = True,
                now: datetime | None = None) -> str:
    term = term or Term(color=False)
    items = list_items(conn, include_closed=include_closed)
    L = [term.bold("WORK ITEMS"), ""]
    if not items:
        L.append("None yet. Create one: runpeek work new \"Name\" --kind feature")
        return "\n".join(L)
    L.append(f"  {'Id':<11}{'Kind':<12}{'Status':<22}{'Sessions':>9}{'Calls':>7}{'Est. cost':>14}  Name")
    for it in items:
        r = build_report(conn, str(it["work_item_id"]))
        status = it["status"] if it["status"] == "open" else f"closed · {it['outcome']}"
        L.append(f"  {it['work_item_id']:<11}{it['kind']:<12}{status:<22}{len(r.sessions):>9}{r.total.calls:>7}"
                 f"{_cost_cell(r.total):>14}  {sanitize(it['name'])[:40]}")
    L.append("")
    L.append("Est. cost: " + COST_LABEL + ". '+?' = some calls could not be priced.")
    L.append(f"Report: runpeek work show {items[0]['work_item_id']}")
    return "\n".join(L)


def render_suggestions(conn: sqlite3.Connection, wid: str, *, term: Term | None = None,
                       now: datetime | None = None) -> str:
    term = term or Term(color=False)
    item = get(conn, wid)
    assert item is not None
    rows = suggest(conn, wid)
    L = [term.bold(f"SUGGESTED SESSIONS FOR {wid}") + f"  ·  {sanitize(item['name'])}"]
    if not item["repository"] and not item["branch"]:
        L.append("  No repository or branch on the work item, so nothing to match on."
                 f" Set one: runpeek work edit {wid} --repository /path --branch name")
        return "\n".join(L)
    if not rows:
        L.append("  No unassigned sessions match the repository or branch.")
        return "\n".join(L)
    L.append("  Nothing is assigned by this command. Review, then: runpeek work assign "
             f"{wid} <session-id>…")
    L.append("")
    L.append(f"  {'Started':<18}{'Session':<14}{'Agent':<12}  Why")
    for r, why in rows:
        L.append(f"  {ui.when(r['first_event_at'] or r['first_seen_at'], now):<18}"
                 f"{str(r['session_id'])[:12]:<14}{_label(str(r['source']))[:11]:<12}  {', '.join(why)}")
    return "\n".join(L)
