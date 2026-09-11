"""runpeek — work · watch · sessions · session · findings · run · summary · events · export · reprice."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__, accounting
from . import perspective as persp
from . import summary as summ
from .ids import new_id
from .money import usd_string
from .rates import RateCardSet, load_card
from .store import apply_schema, open_connection
from .ui import Term

BOOT_DIR = Path(__file__).parent / "_boot"
DEFAULT_DB = Path(".runpeek") / "runpeek.db"

_SECRET_FLAG_WORDS = ("key", "token", "secret", "password", "passwd", "credential", "auth")
_SECRET_PREFIXES = ("sk-", "sk_", "pk_", "rk_", "ghp_", "gho_", "github_pat_", "xox", "akia", "aiza", "ya29.", "eyj")
_COMMAND_MAX_CHARS = 200


def _looks_like_secret(tok: str) -> bool:
    low = tok.lower()
    if low.startswith(_SECRET_PREFIXES):
        return True
    # long single-token strings with no path separators: opaque tokens, hashes, JWTs
    if len(tok) >= 32 and "/" not in tok and "." not in tok.strip("."):
        if sum(ch.isalnum() for ch in tok) / len(tok) > 0.9:
            return True
    return False


def describe_command(argv: list[str]) -> str:
    """A command description safe to store and display.

    This is a HEURISTIC, not a guarantee. It keeps tokens that look like
    program names, paths, modules or plain flags and redacts: inline ``-c``
    programs; anything containing whitespace or JSON/brace payloads; values of
    secret-looking options (``--api-key …``, ``TOKEN=…``); URL userinfo and
    query strings; and positional tokens with known secret prefixes or the
    shape of an opaque credential. A secret that looks like an ordinary word
    or a short path will pass through. New collection paths (the agent
    watcher) do not use this at all — they store allowlisted fields only.
    """
    out: list[str] = []
    redact_next = False
    for tok in argv:
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        if tok == "-c":
            out.append(tok)
            redact_next = True
            continue
        low = tok.lower()
        if any(ch.isspace() for ch in tok) or any(ch in tok for ch in "{}[]"):
            out.append("<redacted>")
            continue
        if "://" in tok:
            out.append(_redact_url(tok))
            continue
        if "=" in tok:
            k, v = tok.split("=", 1)
            if any(w in k.lower() for w in _SECRET_FLAG_WORDS) or _looks_like_secret(v):
                out.append(k + "=<redacted>")
                continue
        if low.startswith("--") and any(w in low for w in _SECRET_FLAG_WORDS):
            out.append(tok)
            redact_next = True
            continue
        if _looks_like_secret(tok):
            out.append("<redacted>")
            continue
        out.append(tok)
    text = " ".join(out)
    if len(text) > _COMMAND_MAX_CHARS:
        text = text[: _COMMAND_MAX_CHARS - 1] + "…"
    return text


def _redact_url(tok: str) -> str:
    """Keep scheme, host and path; drop userinfo, query and fragment."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(tok)
    except ValueError:
        return "<redacted>"
    host = u.hostname or ""
    port = f":{u.port}" if u.port else ""
    marker = "<redacted>@" if u.username or u.password else ""
    tail = "?<redacted>" if u.query else ""
    return f"{u.scheme}://{marker}{host}{port}{u.path}{tail}"


def _term(args: argparse.Namespace) -> Term:
    color = False if getattr(args, "no_color", False) else None
    return Term(color=color)


LEGACY_DB = Path(".nemulai") / "nemulai.db"
_legacy_notice_shown = False


def resolve_db(explicit: str | None) -> tuple[Path, str | None]:
    """Store path and an optional one-line notice.

    Order: --db, $RUNPEEK_DB, $NEMULAI_DB (legacy), ./.runpeek/runpeek.db,
    then — only if that does not exist yet — an existing ./.nemulai/nemulai.db.
    A legacy store is used in place, never copied or overwritten, so data
    collected before the rename stays visible. See docs/MIGRATION.md.
    """
    if explicit:
        return Path(explicit), None
    if os.environ.get("RUNPEEK_DB"):
        return Path(os.environ["RUNPEEK_DB"]), None
    if os.environ.get("NEMULAI_DB"):
        return Path(os.environ["NEMULAI_DB"]), "using $NEMULAI_DB (legacy name; set RUNPEEK_DB instead)"
    if not DEFAULT_DB.exists() and LEGACY_DB.exists():
        return LEGACY_DB, (f"using legacy store {LEGACY_DB} — move it to {DEFAULT_DB} to silence this"
                           " (docs/MIGRATION.md)")
    return DEFAULT_DB, None


def _db(args: argparse.Namespace) -> Path:
    global _legacy_notice_shown
    path, note = resolve_db(getattr(args, "db", None))
    if note and not _legacy_notice_shown:
        _legacy_notice_shown = True
        sys.stderr.write(f"runpeek: {note}\n")
    return path


def _cards(args: argparse.Namespace) -> RateCardSet:
    extra = [load_card(Path(p)) for p in (getattr(args, "rate_card", None) or [])]
    return RateCardSet.builtin(extra)


def _perspective(args: argparse.Namespace, conn: sqlite3.Connection) -> persp.Perspective:
    name = getattr(args, "perspective", None) or "default"
    if name == "default":
        return persp.DEFAULT
    p = persp.load(conn, name)
    if p is None:
        sys.exit(f"runpeek: unknown perspective {name!r}; create one with `runpeek reprice --pin <rate_card_id>`")
    return p


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    path = _db(args)
    if not path.exists():
        sys.exit(f"runpeek: no store at {path} (run something with `runpeek run` first, or pass --db)")
    conn = open_connection(path)
    apply_schema(conn)
    return conn


def _resolve_run(args: argparse.Namespace, conn: sqlite3.Connection) -> str | None:
    if getattr(args, "all_runs", False):
        return None
    if getattr(args, "run", None):
        return str(args.run)
    return summ.latest_run_id(conn)


# ----------------------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace) -> int:
    cmd: list[str] = [c for c in args.cmd if c != "--"] if args.cmd else []
    if not cmd:
        sys.exit("runpeek run: give a command, e.g. `runpeek run python app.py`")
    db = _db(args)
    run_id = new_id("run")
    env = dict(os.environ)
    env["RUNPEEK_ENABLED"] = "1"
    env["RUNPEEK_DB"] = str(db)
    env["RUNPEEK_RUN_ID"] = run_id
    env["RUNPEEK_COMMAND"] = describe_command(cmd)
    env["PYTHONPATH"] = str(BOOT_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    try:
        proc = subprocess.Popen(cmd, env=env)
    except FileNotFoundError:
        sys.exit(f"runpeek run: command not found: {cmd[0]}")

    def forward(signum: int, _frame: Any) -> None:
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    previous = {s: signal.signal(s, forward) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        rc = proc.wait()
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
    exit_code = 128 - rc if rc < 0 else rc  # negative rc = killed by signal -rc

    if db.exists():
        try:
            conn = open_connection(db)
            apply_schema(conn)
            # The application's exit status is a fact the parent knows and the
            # child cannot record for itself; it is separate from whether the
            # child's telemetry shut down cleanly.
            conn.execute("UPDATE runs SET app_exit_status = ? WHERE run_id = ?", (rc, run_id))
            if not args.no_summary:
                accounting.run(conn, persp.DEFAULT, _cards(args))
                print()
                print(summ.render(summ.build(conn, run_id, persp.DEFAULT), verbose=args.verbose, term=_term(args)))
            conn.close()
        except sqlite3.Error as exc:
            sys.stderr.write(f"runpeek: could not read store for summary: {exc}\n")
    if not db.exists():
        sys.stderr.write(
            "runpeek: no store was created. The command may not have started a Python interpreter that "
            "processes site, or `runpeek` is not importable by that interpreter.\n"
        )
    return exit_code


# ----------------------------------------------------------------------------- summary / events / export


def cmd_summary(args: argparse.Namespace) -> int:
    conn = _open(args)
    p = _perspective(args, conn)
    accounting.run(conn, p, _cards(args))
    run_id = _resolve_run(args, conn)
    print(summ.render(summ.build(conn, run_id, p), verbose=args.verbose, term=_term(args), heading="SUMMARY"))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    conn = _open(args)
    p = _perspective(args, conn)
    accounting.run(conn, p, _cards(args))
    run_id = _resolve_run(args, conn)
    f, params = ("", ()) if run_id is None else (" WHERE a.run_id = ?", (run_id,))
    rows = conn.execute(
        "SELECT a.started_at, a.observed_status, COALESCE(a.model_served, a.model_requested) AS model,"
        " o.input_tokens, o.output_tokens, e.amount_nanos, e.estimation_status, op.customer_id, op.job_name,"
        " op.attribution_state, a.error_class"
        " FROM attempts a JOIN operations op ON op.operation_id = a.operation_id"
        " LEFT JOIN source_observations o ON o.attempt_id = a.attempt_id AND o.selected = 1"
        " LEFT JOIN charges c ON c.attempt_id = a.attempt_id"
        " LEFT JOIN cost_estimates e ON e.charge_id = c.charge_id AND e.current = 1 AND e.perspective_id = ?"
        f"{f} ORDER BY a.started_at DESC LIMIT ?",
        (p.perspective_id, *params, args.last),
    ).fetchall()
    print(f"{'started_at':<32} {'status':<16} {'model':<22} {'in':>7} {'out':>7} {'cost':>14} customer/job")
    for r in reversed(rows):
        cost = usd_string(r["amount_nanos"]) if r["amount_nanos"] is not None else (r["estimation_status"] or "—")
        who = f"{r['customer_id'] or '-'} / {r['job_name'] or '-'} [{r['attribution_state']}]"
        extra = f" {r['error_class']}" if r["error_class"] else ""
        print(f"{(r['started_at'] or '')[:32]:<32} {r['observed_status'] + extra:<16} {(r['model'] or '')[:22]:<22}"
              f" {r['input_tokens'] if r['input_tokens'] is not None else '—':>7}"
              f" {r['output_tokens'] if r['output_tokens'] is not None else '—':>7} {cost:>14} {who}")
    return 0


EXPORT_TABLES = ("runs", "spans", "operations", "attempts", "identifiers", "source_observations",
                 "correlations", "charges", "perspectives", "cost_estimates", "health_events",
                 # coding-agent observer (no run_id; always exported whole). `meta` is never exported:
                 # it holds the fingerprint key.
                 "agent_sessions", "agent_turns", "agent_actions", "agent_usage", "agent_usage_duplicates",
                 "agent_findings", "watch_checkpoints",
                 "work_items", "work_item_sessions", "work_item_events")


def cmd_export(args: argparse.Namespace) -> int:
    conn = _open(args)
    p = _perspective(args, conn)
    accounting.run(conn, p, _cards(args))
    run_id = _resolve_run(args, conn)
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        for table in EXPORT_TABLES:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
            has_run = "run_id" in cols
            sql = f"SELECT * FROM {table}"
            params: tuple[Any, ...] = ()
            if run_id and has_run:
                sql += " WHERE run_id = ?"
                params = (run_id,)
            for row in conn.execute(sql, params):
                rec = {"record": table, **dict(row)}
                if table == "cost_estimates" and rec.get("amount_nanos") is not None:
                    rec["amount_usd"] = usd_string(int(rec["amount_nanos"]))
                out.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


def cmd_reprice(args: argparse.Namespace) -> int:
    conn = _open(args)
    cards = _cards(args)
    if args.pin not in cards.ids():
        sys.exit(f"runpeek reprice: unknown rate card {args.pin!r}; known: {', '.join(cards.ids())}")
    p = persp.pinned(args.pin)
    persp.ensure(conn, p)
    accounting.run(conn, persp.DEFAULT, cards)  # keep the default up to date and untouched
    result = accounting.run(conn, p, cards)
    run_id = _resolve_run(args, conn)
    print(f"perspective {p.perspective_id}: {result['estimates_created']} estimate revisions created "
          f"(re-running with identical inputs creates none)")
    print()
    print(summ.render(summ.build(conn, run_id, p)))
    return 0


# ----------------------------------------------------------------------------- agent observer


def _open_or_create(args: argparse.Namespace) -> sqlite3.Connection:
    path = _db(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    existed = path.exists()
    conn = open_connection(path)
    apply_schema(conn)
    if not existed:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


def _project_arg(args: argparse.Namespace) -> str | None:
    if getattr(args, "all_projects", False):
        return None
    return str(Path(args.project).resolve()) if getattr(args, "project", None) else os.getcwd()


def cmd_watch(args: argparse.Namespace) -> int:
    from .agents.ingest import SOURCES
    from .agents.watch import Watcher

    sources = SOURCES if args.source == "all" else (args.source,)
    conn = _open_or_create(args)
    w = Watcher(conn, project=_project_arg(args), all_projects=args.all_projects, history=args.history,
                interval_s=args.interval, cards=_cards(args), verbose=args.verbose, term=_term(args),
                sources=sources)
    w.install_signal_handlers()
    w.run(once=args.once)
    conn.close()
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from .agents.report import render_sessions

    conn = _open(args)
    print(render_sessions(conn, _project_arg(args), args.last, include_empty=args.include_empty,
                          detailed=args.detailed, unassigned=args.unassigned, term=_term(args)))
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    from .agents.report import render_session, resolve_session_id

    conn = _open(args)
    resolved = resolve_session_id(conn, args.session_id)
    if isinstance(resolved, list):
        if not resolved:
            sys.exit(f"runpeek session: no session matches {args.session_id!r}. List them: runpeek sessions")
        opts = ", ".join(r.split("/")[-1][:12] for r in resolved[:6])
        sys.exit(f"runpeek session: {args.session_id!r} matches {len(resolved)} sessions ({opts}"
                 f"{', …' if len(resolved) > 6 else ''}). Use a longer prefix.")
    sid = resolved
    if args.set_customer is not None or args.set_job is not None:
        conn.execute("UPDATE agent_sessions SET customer_id = COALESCE(?, customer_id),"
                     " job_name = COALESCE(?, job_name) WHERE session_id = ?", (args.set_customer, args.set_job, sid))
        print(f"session {sid[:8]} mapped explicitly: customer {args.set_customer or '(unchanged)'},"
              f" job {args.set_job or '(unchanged)'}")
    print(render_session(conn, sid, findings_only=args.findings_only, term=_term(args)))
    return 0


def cmd_findings(args: argparse.Namespace) -> int:
    from .agents.report import render_findings

    conn = _open(args)
    print(render_findings(conn, _project_arg(args), args.last, term=_term(args)))
    return 0


# ----------------------------------------------------------------------------- work items


def _resolve_work(conn: sqlite3.Connection, ref: str) -> str:
    from .agents.work import resolve

    got = resolve(conn, ref)
    if isinstance(got, list):
        if not got:
            sys.exit(f"runpeek work: no work item matches {ref!r}. List them: runpeek work list")
        sys.exit(f"runpeek work: {ref!r} matches {len(got)} work items ({', '.join(got[:6])}). Use the full id.")
    return got


def _resolve_sessions(conn: sqlite3.Connection, refs: list[str]) -> list[str]:
    from .agents.report import resolve_session_id

    out: list[str] = []
    for ref in refs:
        got = resolve_session_id(conn, ref)
        if isinstance(got, list):
            if not got:
                sys.exit(f"runpeek work: no session matches {ref!r}. List them: runpeek sessions --all-projects")
            opts = ", ".join(r.split("/")[-1][:12] for r in got[:6])
            sys.exit(f"runpeek work: {ref!r} matches {len(got)} sessions ({opts}). Use a longer prefix.")
        out.append(got)
    return out


def cmd_work_new(args: argparse.Namespace) -> int:
    from .agents import work

    conn = _open_or_create(args)
    try:
        wid = work.create(conn, args.name, args.kind, repository=args.repository, issue=args.issue,
                          branch=args.branch, pr=args.pr, deployment=args.deployment, note=args.note)
    except work.WorkItemError as exc:
        sys.exit(f"runpeek work new: {exc}")
    print(f"created {wid}  ·  {args.name}  ·  {args.kind}")
    print(f"Assign sessions: runpeek work assign {wid} <session-id>…   Suggestions: runpeek work suggest {wid}")
    return 0


def cmd_work_list(args: argparse.Namespace) -> int:
    from .agents.work import render_list

    conn = _open(args)
    print(render_list(conn, term=_term(args), include_closed=not args.open))
    return 0


def cmd_work_show(args: argparse.Namespace) -> int:
    from .agents import work

    conn = _open(args)
    wid = _resolve_work(conn, args.work_item)
    try:
        r = work.build_report(conn, wid, cards=_cards(args), pin=args.pin)
    except work.WorkItemError as exc:
        sys.exit(f"runpeek work show: {exc}")
    print(work.render_report(r, term=_term(args), trace=args.trace, conn=conn))
    return 0


def cmd_work_assign(args: argparse.Namespace) -> int:
    from .agents import work

    conn = _open(args)
    wid = _resolve_work(conn, args.work_item)
    sids = _resolve_sessions(conn, args.sessions)
    try:
        result = work.assign(conn, wid, sids)
    except work.WorkItemError as exc:
        sys.exit(f"runpeek work assign: {exc}")
    for sid, prev in result:
        short = sid.split("/")[-1][:12]
        if prev == wid:
            print(f"{short}: already on {wid}")
        elif prev:
            print(f"{short}: reassigned {prev} → {wid}")
        else:
            print(f"{short}: assigned to {wid}")
    print(f"Report: runpeek work show {wid}")
    return 0


def cmd_work_unassign(args: argparse.Namespace) -> int:
    from .agents import work

    conn = _open(args)
    sids = _resolve_sessions(conn, args.sessions)
    for sid, prev in work.unassign(conn, sids):
        short = sid.split("/")[-1][:12]
        print(f"{short}: removed from {prev}" if prev else f"{short}: was not assigned")
    return 0


def cmd_work_status(args: argparse.Namespace) -> int:
    from .agents import work

    conn = _open(args)
    wid = _resolve_work(conn, args.work_item)
    status = args.status
    outcome = args.outcome
    if outcome and status is None:
        status = "closed"
    try:
        work.set_status(conn, wid, status=status, outcome=outcome)
    except work.WorkItemError as exc:
        sys.exit(f"runpeek work status: {exc}")
    item = work.get(conn, wid)
    assert item is not None
    print(f"{wid}: {work.outcome_line(dict(item))}")
    return 0


def cmd_work_edit(args: argparse.Namespace) -> int:
    from .agents import work

    conn = _open(args)
    wid = _resolve_work(conn, args.work_item)
    try:
        work.edit(conn, wid, name=args.name, kind=args.kind, repository=args.repository, issue=args.issue,
                  branch=args.branch, pr=args.pr, deployment=args.deployment, note=args.note)
    except work.WorkItemError as exc:
        sys.exit(f"runpeek work edit: {exc}")
    print(f"{wid}: updated")
    return 0


def cmd_work_suggest(args: argparse.Namespace) -> int:
    from .agents.work import render_suggestions

    conn = _open(args)
    wid = _resolve_work(conn, args.work_item)
    print(render_suggestions(conn, wid, term=_term(args)))
    return 0


# ----------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="runpeek",
        description="RunPeek by NemulAI — see where your coding agents spend money. Groups Claude Code and"
                    " Codex sessions into work items (a task, feature, bug fix or deployment), estimates their"
                    " model cost locally with dated list prices, shows what drove it and how complete the"
                    " accounting is. Also observes the OpenAI Python SDK in your own applications.",
    )
    ap.add_argument("--version", action="version", version=f"runpeek {__version__}")
    ap.add_argument("--no-color", action="store_true", help="plain output (also honoured: NO_COLOR, non-TTY)")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, *, perspective: bool = True) -> None:
        sp.add_argument("--db", help="store path (default ./.runpeek/runpeek.db or $RUNPEEK_DB)")
        sp.add_argument("--rate-card", action="append", metavar="FILE", help="extra rate card JSON (repeatable)")
        if perspective:
            sp.add_argument("--perspective", default="default")

    wk = sub.add_parser("work", help="work items: group agent sessions into a task, feature, bug fix or deployment")
    wsub = wk.add_subparsers(dest="work_command", required=True)

    wn = wsub.add_parser("new", help="create a work item")
    common(wn, perspective=False)
    wn.add_argument("name")
    wn.add_argument("--kind", choices=["task", "feature", "bugfix", "deployment"], default="task")
    wn.add_argument("--repository", help="project path or repository url (used only to suggest sessions)")
    wn.add_argument("--issue")
    wn.add_argument("--branch")
    wn.add_argument("--pr")
    wn.add_argument("--deployment")
    wn.add_argument("--note")
    wn.set_defaults(fn=cmd_work_new)

    wl = wsub.add_parser("list", help="list work items with their estimated model cost")
    common(wl, perspective=False)
    wl.add_argument("--open", action="store_true", help="open items only")
    wl.set_defaults(fn=cmd_work_list)

    wsh = wsub.add_parser("show", help="cost report for one work item")
    common(wsh, perspective=False)
    wsh.add_argument("work_item")
    wsh.add_argument("--trace", type=int, nargs="?", const=50, default=0, metavar="N",
                     help="also list the last N source usage records with ids and pricing")
    wsh.add_argument("--pin", metavar="RATE_CARD_ID",
                     help="price every call under one rate card for this view only (stored estimates untouched)")
    wsh.set_defaults(fn=cmd_work_show)

    wa = wsub.add_parser("assign", help="attach sessions to a work item (reassigns if already elsewhere)")
    common(wa, perspective=False)
    wa.add_argument("work_item")
    wa.add_argument("sessions", nargs="+", metavar="SESSION")
    wa.set_defaults(fn=cmd_work_assign)

    wu = wsub.add_parser("unassign", help="detach sessions from whatever work item they are on")
    common(wu, perspective=False)
    wu.add_argument("sessions", nargs="+", metavar="SESSION")
    wu.set_defaults(fn=cmd_work_unassign)

    wst = wsub.add_parser("status", help="set status and outcome")
    common(wst, perspective=False)
    wst.add_argument("work_item")
    wst.add_argument("--status", choices=["open", "closed"])
    wst.add_argument("--outcome", choices=["completed", "incomplete", "failed", "abandoned"],
                     help="closes the item unless --status open is given")
    wst.set_defaults(fn=cmd_work_status)

    we = wsub.add_parser("edit", help="change name, kind or references")
    common(we, perspective=False)
    we.add_argument("work_item")
    we.add_argument("--name")
    we.add_argument("--kind", choices=["task", "feature", "bugfix", "deployment"])
    we.add_argument("--repository")
    we.add_argument("--issue")
    we.add_argument("--branch")
    we.add_argument("--pr")
    we.add_argument("--deployment")
    we.add_argument("--note")
    we.set_defaults(fn=cmd_work_edit)

    wsu = wsub.add_parser("suggest", help="unassigned sessions that match the item's repository or branch")
    common(wsu, perspective=False)
    wsu.add_argument("work_item")
    wsu.set_defaults(fn=cmd_work_suggest)

    r = sub.add_parser("run", help="run a command with the harness attached, then print its summary")
    common(r, perspective=False)
    r.add_argument("--no-summary", action="store_true")
    r.add_argument("--verbose", action="store_true", help="full accounting view instead of the plain summary")
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("summary", help="economic summary of a run (default: latest)")
    common(s)
    s.add_argument("--run")
    s.add_argument("--all-runs", action="store_true")
    s.add_argument("--verbose", action="store_true", help="full accounting view: buckets, perspective, rate cards")
    s.set_defaults(fn=cmd_summary)

    e = sub.add_parser("events", help="recent attempts")
    common(e)
    e.add_argument("--run")
    e.add_argument("--all-runs", action="store_true")
    e.add_argument("--last", type=int, default=20)
    e.set_defaults(fn=cmd_events)

    x = sub.add_parser("export", help="export records as JSONL")
    common(x)
    x.add_argument("--run")
    x.add_argument("--all-runs", action="store_true")
    x.add_argument("--format", choices=["jsonl"], default="jsonl")
    x.add_argument("--out")
    x.set_defaults(fn=cmd_export)

    rp = sub.add_parser("reprice", help="price under a separate pinned perspective; the default is preserved")
    common(rp, perspective=False)
    rp.add_argument("--pin", required=True, metavar="RATE_CARD_ID")
    rp.add_argument("--run")
    rp.add_argument("--all-runs", action="store_true")
    rp.set_defaults(fn=cmd_reprice)

    w = sub.add_parser("watch", help="observe Claude Code and Codex sessions (foreground process)")
    common(w, perspective=False)
    w.add_argument("--source", default="all", choices=["all", "claude-code", "codex"],
                   help="which agent's session records to read (default: all supported)")
    w.add_argument("--project", help="project directory to watch (default: current directory)")
    w.add_argument("--all-projects", action="store_true", help="watch every project the agent has sessions for")
    w.add_argument("--history", default="7d",
                   help="initial history: none | all | <N>d | <N>h (default 7d: files modified in the last 7 days)")
    w.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    w.add_argument("--once", action="store_true", help="ingest what exists now and exit")
    w.add_argument("--verbose", action="store_true", help="show polling internals and per-file ingest counts")
    w.set_defaults(fn=cmd_watch)

    ss = sub.add_parser("sessions", help="list observed coding-agent sessions")
    common(ss, perspective=False)
    ss.add_argument("--project")
    ss.add_argument("--all-projects", action="store_true")
    ss.add_argument("--last", type=int, default=20)
    ss.add_argument("--include-empty", action="store_true", help="also list transcripts with no activity")
    ss.add_argument("--detailed", action="store_true", help="token and estimated-cost columns")
    ss.add_argument("--unassigned", action="store_true", help="only sessions not on any work item")
    ss.set_defaults(fn=cmd_sessions)

    so = sub.add_parser("session", help="one session: usage, turns, actions, findings")
    common(so, perspective=False)
    so.add_argument("session_id")
    so.add_argument("--findings-only", action="store_true")
    so.add_argument("--set-customer", metavar="CUSTOMER", help="map this session to a customer explicitly")
    so.add_argument("--set-job", metavar="JOB")
    so.set_defaults(fn=cmd_session)

    fd = sub.add_parser("findings", help="potential inefficiencies across sessions")
    common(fd, perspective=False)
    fd.add_argument("--project")
    fd.add_argument("--all-projects", action="store_true")
    fd.add_argument("--last", type=int, default=50)
    fd.set_defaults(fn=cmd_findings)
    from .unified_cli import register
    register(sub)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.fn(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
