"""nemulai — run · summary · events · export · reprice."""

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

BOOT_DIR = Path(__file__).parent / "_boot"
DEFAULT_DB = Path(".nemulai") / "nemulai.db"

_SECRET_FLAG_WORDS = ("key", "token", "secret", "password", "passwd", "credential", "auth")
_COMMAND_MAX_CHARS = 200


def describe_command(argv: list[str]) -> str:
    """A command description safe to store and display.

    The recorded command is metadata, and metadata can leak: ``python -c
    "<program>"`` carries source that may embed prompts or secrets, and
    ``--api-key=...`` carries the secret itself. Rule: keep tokens that look
    like program names, paths, modules or plain flags; redact inline code,
    anything multi-line or whitespace-bearing, and values of secret-looking
    options. Length is capped. This is a description, not a transcript.
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
        if any(ch.isspace() for ch in tok):
            out.append("<redacted>")
            continue
        if "=" in tok and any(w in low.split("=", 1)[0] for w in _SECRET_FLAG_WORDS):
            out.append(tok.split("=", 1)[0] + "=<redacted>")
            continue
        if low.startswith("--") and any(w in low for w in _SECRET_FLAG_WORDS):
            out.append(tok)
            redact_next = True
            continue
        out.append(tok)
    text = " ".join(out)
    if len(text) > _COMMAND_MAX_CHARS:
        text = text[: _COMMAND_MAX_CHARS - 1] + "…"
    return text


def _db(args: argparse.Namespace) -> Path:
    return Path(args.db or os.environ.get("NEMULAI_DB") or DEFAULT_DB)


def _cards(args: argparse.Namespace) -> RateCardSet:
    extra = [load_card(Path(p)) for p in (getattr(args, "rate_card", None) or [])]
    return RateCardSet.builtin(extra)


def _perspective(args: argparse.Namespace, conn: sqlite3.Connection) -> persp.Perspective:
    name = getattr(args, "perspective", None) or "default"
    if name == "default":
        return persp.DEFAULT
    p = persp.load(conn, name)
    if p is None:
        sys.exit(f"nemulai: unknown perspective {name!r}; create one with `nemulai reprice --pin <rate_card_id>`")
    return p


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    path = _db(args)
    if not path.exists():
        sys.exit(f"nemulai: no store at {path} (run something with `nemulai run` first, or pass --db)")
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
        sys.exit("nemulai run: give a command, e.g. `nemulai run python app.py`")
    db = _db(args)
    run_id = new_id("run")
    env = dict(os.environ)
    env["NEMULAI_ENABLED"] = "1"
    env["NEMULAI_DB"] = str(db)
    env["NEMULAI_RUN_ID"] = run_id
    env["NEMULAI_COMMAND"] = describe_command(cmd)
    env["PYTHONPATH"] = str(BOOT_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    try:
        proc = subprocess.Popen(cmd, env=env)
    except FileNotFoundError:
        sys.exit(f"nemulai run: command not found: {cmd[0]}")

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
                print(summ.render(summ.build(conn, run_id, persp.DEFAULT)))
            conn.close()
        except sqlite3.Error as exc:
            sys.stderr.write(f"nemulai: could not read store for summary: {exc}\n")
    if not db.exists():
        sys.stderr.write(
            "nemulai: no store was created. The command may not have started a Python interpreter that "
            "processes site, or `nemulai` is not importable by that interpreter.\n"
        )
    return exit_code


# ----------------------------------------------------------------------------- summary / events / export


def cmd_summary(args: argparse.Namespace) -> int:
    conn = _open(args)
    p = _perspective(args, conn)
    accounting.run(conn, p, _cards(args))
    run_id = _resolve_run(args, conn)
    print(summ.render(summ.build(conn, run_id, p)))
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
                 "correlations", "charges", "perspectives", "cost_estimates", "health_events")


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
        sys.exit(f"nemulai reprice: unknown rate card {args.pin!r}; known: {', '.join(cards.ids())}")
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


# ----------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nemulai", description="Local-first AI workload economics.")
    ap.add_argument("--version", action="version", version=f"nemulai {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, *, perspective: bool = True) -> None:
        sp.add_argument("--db", help="store path (default ./.nemulai/nemulai.db or $NEMULAI_DB)")
        sp.add_argument("--rate-card", action="append", metavar="FILE", help="extra rate card JSON (repeatable)")
        if perspective:
            sp.add_argument("--perspective", default="default")

    r = sub.add_parser("run", help="run a command with the harness attached, then print its summary")
    common(r, perspective=False)
    r.add_argument("--no-summary", action="store_true")
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("summary", help="economic summary of a run (default: latest)")
    common(s)
    s.add_argument("--run")
    s.add_argument("--all-runs", action="store_true")
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
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.fn(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
