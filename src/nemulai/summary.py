"""The economic summary: every displayed bucket reconciles.

Denominators, stated once:
  operations / attempts   all attempts recorded (one per operation in M1)
  usage coverage          completed attempts (denominator) → exact / missing
  pricing coverage        attempts with usage (denominator) → priced / partial / unpriced
  attribution coverage    known estimated cost (priced + partial amounts) → attributed / job_only / unattributed
  billing                 charges → expected / unknown / confirmed / not_billed
The headline figure is *known estimated cost* under one perspective. It is
not total actual spend: unpriced, no-usage, in-progress and unknown-billing
counts are printed beside it, never folded into it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .money import format_usd
from .perspective import Perspective


@dataclass
class Summary:
    run_id: str | None
    run: dict[str, Any] | None
    perspective: Perspective
    rate_cards: list[str]
    calc_version: int | None
    adapters: list[str]
    operations: int = 0
    attempts: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    in_progress: int = 0
    unsupported: int = 0
    start_missing: int = 0
    # estimates (current, this perspective)
    charges: int = 0
    est_counts: dict[str, int] = field(default_factory=dict)  # priced/partial/unpriced/no_usage
    known_cost_nanos: int = 0
    partial_cost_nanos: int = 0
    unpriced_models: dict[str, int] = field(default_factory=dict)
    fallback_priced: int = 0
    billing_counts: dict[str, int] = field(default_factory=dict)
    usage_exact: int = 0
    usage_missing: int = 0
    attribution_cost: dict[str, int] = field(default_factory=dict)
    by_customer: list[dict[str, Any]] = field(default_factory=list)
    by_model: list[dict[str, Any]] = field(default_factory=list)
    conflicting: int = 0
    inferred_unresolved: int = 0
    hook_failures: int = 0
    unsupported_events: int = 0
    counters: dict[str, Any] = field(default_factory=dict)
    persisted_rows: int = 0  # counted from the store, independent of the run's final counters

    @property
    def with_usage(self) -> int:
        return self.usage_exact


def _run_filter(run_id: str | None, alias: str) -> tuple[str, tuple[Any, ...]]:
    return (f" AND {alias}.run_id = ?", (run_id,)) if run_id else ("", ())


def latest_run_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def build(conn: sqlite3.Connection, run_id: str | None, perspective: Perspective) -> Summary:
    run = None
    if run_id:
        r = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        run = dict(r) if r else None
    s = Summary(run_id=run_id, run=run, perspective=perspective, rate_cards=[], calc_version=None, adapters=[])
    f, p = _run_filter(run_id, "a")
    fo, po = _run_filter(run_id, "o")

    s.operations = conn.execute(f"SELECT COUNT(*) FROM operations o WHERE 1=1{fo}", po).fetchone()[0]
    for row in conn.execute(
        f"SELECT observed_status, COUNT(*) AS n FROM attempts a WHERE 1=1{f} GROUP BY observed_status", p
    ):
        s.status_counts[row["observed_status"]] = row["n"]
    s.attempts = sum(s.status_counts.values())
    s.in_progress = s.status_counts.get("in_progress", 0)
    s.unsupported = s.status_counts.get("unsupported_stream", 0)
    s.start_missing = conn.execute(
        f"SELECT COUNT(*) FROM attempts a WHERE start_missing = 1{f}", p
    ).fetchone()[0]

    # usage coverage over completed attempts
    for row in conn.execute(
        f"SELECT o.usage_source AS u, COUNT(*) AS n FROM attempts a"
        f" JOIN source_observations o ON o.attempt_id = a.attempt_id AND o.selected = 1"
        f" WHERE a.observed_status = 'completed'{f} GROUP BY o.usage_source",
        p,
    ):
        if row["u"] == "missing":
            s.usage_missing += row["n"]
        else:
            s.usage_exact += row["n"]
    completed_no_obs = conn.execute(
        f"SELECT COUNT(*) FROM attempts a WHERE a.observed_status = 'completed'{f}"
        f" AND NOT EXISTS (SELECT 1 FROM source_observations o WHERE o.attempt_id = a.attempt_id AND o.selected = 1)",
        p,
    ).fetchone()[0]
    s.usage_missing += completed_no_obs

    est_sql = (
        "SELECT e.*, a.attempt_id, a.model_served, a.model_requested, c.billing_status,"
        " op.attribution_state, op.customer_id, op.job_name,"
        " o.input_tokens, o.output_tokens, o.cached_input_tokens"
        " FROM cost_estimates e JOIN charges c ON c.charge_id = e.charge_id"
        " JOIN attempts a ON a.attempt_id = c.attempt_id"
        " JOIN operations op ON op.operation_id = a.operation_id"
        " LEFT JOIN source_observations o ON o.observation_id = e.selected_observation_id"
        f" WHERE e.current = 1 AND e.perspective_id = ?{f}"
    )
    rows = conn.execute(est_sql, (perspective.perspective_id, *p)).fetchall()
    cards: set[str] = set()
    cust: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    for r in rows:
        s.charges += 1
        st = r["estimation_status"]
        s.est_counts[st] = s.est_counts.get(st, 0) + 1
        s.billing_counts[r["billing_status"]] = s.billing_counts.get(r["billing_status"], 0) + 1
        if r["rate_card_id"]:
            cards.add(r["rate_card_id"])
        s.calc_version = r["calc_version"]
        if r["rate_resolution"] and r["rate_resolution"].startswith("fallback"):
            s.fallback_priced += 1
        amount = r["amount_nanos"]
        model = r["model_served"] or r["model_requested"] or "(unknown)"
        m = models.setdefault(model, {"model": model, "ops": 0, "in": 0, "out": 0, "cached": 0, "cost": 0,
                                      "priced": 0, "unpriced": 0, "no_usage": 0})
        m["ops"] += 1
        m["in"] += r["input_tokens"] or 0
        m["out"] += r["output_tokens"] or 0
        m["cached"] += r["cached_input_tokens"] or 0
        if st == "unpriced":
            s.unpriced_models[model] = s.unpriced_models.get(model, 0) + 1
            m["unpriced"] += 1
        elif st == "no_usage":
            m["no_usage"] += 1
        if amount is not None:
            m["priced"] += 1
            m["cost"] += amount
            if st == "partial":
                s.partial_cost_nanos += amount
            s.known_cost_nanos += amount
            state = r["attribution_state"]
            s.attribution_cost[state] = s.attribution_cost.get(state, 0) + amount
        key = r["customer_id"] if r["attribution_state"] == "attributed" else (
            "(job only)" if r["attribution_state"] == "job_only" else "(unattributed)"
        )
        c = cust.setdefault(key, {"customer": key, "ops": 0, "cost": 0, "unmeasured": 0})
        c["ops"] += 1
        if amount is not None:
            c["cost"] += amount
        else:
            c["unmeasured"] += 1
    s.rate_cards = sorted(cards)
    total = s.known_cost_nanos
    for c in cust.values():
        c["share"] = (c["cost"] / total) if total else 0.0
    s.by_customer = sorted(cust.values(), key=lambda c: (-c["cost"], c["customer"]))
    s.by_model = sorted(models.values(), key=lambda m: (-m["cost"], m["model"]))

    s.conflicting = conn.execute("SELECT COUNT(*) FROM correlations WHERE class = 'conflicting'").fetchone()[0]
    s.inferred_unresolved = conn.execute("SELECT COUNT(*) FROM correlations WHERE class = 'inferred'").fetchone()[0]
    fh, ph = _run_filter(run_id, "h")
    health_sql = f"SELECT kind, detail, COUNT(*) AS n FROM health_events h WHERE 1=1{fh} GROUP BY kind, detail"
    for row in conn.execute(health_sql, ph):
        if row["kind"].startswith("hook_failure"):
            s.hook_failures += row["n"]
        elif row["kind"] == "unsupported_surface":
            s.unsupported_events += row["n"]
        elif row["kind"] == "adapter_installed":
            s.adapters.append(_adapter_name(row["detail"]))
    if run:
        s.counters = {
            "dropped": run.get("dropped_confirmed"),
            "unflushed": run.get("unflushed_known"),
            "persist_failures": run.get("persist_failures"),
            "records_written": run.get("records_written"),
        }
        s.persisted_rows = sum(
            conn.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id = ?", (run_id,)).fetchone()[0]
            for t in ("spans", "operations", "attempts", "health_events")
        )
        s.persisted_rows += conn.execute(
            "SELECT COUNT(*) FROM source_observations o JOIN attempts a ON a.attempt_id = o.attempt_id"
            " WHERE a.run_id = ?",
            (run_id,),
        ).fetchone()[0]
        s.persisted_rows += conn.execute(
            "SELECT COUNT(*) FROM identifiers WHERE subject_id IN ("
            "  SELECT operation_id FROM operations WHERE run_id = ?"
            "  UNION SELECT attempt_id FROM attempts WHERE run_id = ?)",
            (run_id, run_id),
        ).fetchone()[0]
    return s


def _signal_name(num: int) -> str:
    try:
        import signal

        return signal.Signals(num).name
    except (ValueError, ImportError):
        return f"signal {num}"


def _app_status(run: dict[str, Any]) -> str:
    if "app_exit_status" not in run or run.get("app_exit_status") is None:
        return "application exit: unknown (not launched by `nemulai run`)"
    rc = int(run["app_exit_status"])
    if rc < 0:
        return f"application: killed by {_signal_name(-rc)}"
    return f"application exit: {rc}" + ("" if rc == 0 else " (failed)")


def _telemetry_status(run: dict[str, Any]) -> str:
    if run.get("ended_at") and run.get("clean_exit"):
        return "telemetry: ended cleanly"
    return "telemetry: DID NOT END CLEANLY (no final record; loss after last heartbeat is unknown)"


def _adapter_name(detail_json: str | None) -> str:
    import json

    try:
        d = json.loads(detail_json or "{}")
        return str(d.get("adapter", "?"))
    except Exception:
        return "?"


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "—"


def render(s: Summary) -> str:
    L: list[str] = []
    run = s.run or {}
    head = f"nemulai · run {s.run_id or 'all runs'}"
    if run.get("command"):
        head += f"  ·  {run['command']}"
    L.append(head)
    if run:
        L.append(f"{_app_status(run)}  ·  {_telemetry_status(run)}")
    L.append(
        f"perspective {s.perspective.perspective_id}  ·  rates {s.perspective.rates}"
        f"  ·  cards {', '.join(s.rate_cards) or 'none used'}"
        + (f"  ·  pinned {s.perspective.pin}" if s.perspective.pin else "")
        + (f"  ·  calc {s.calc_version}" if s.calc_version else "")
    )
    L.append("")
    adapters = ", ".join(s.adapters) or "none recorded"
    L.append(f"OPERATIONS        {s.attempts} attempts ({s.operations} operations)        adapters: {adapters}")
    parts = [f"completed {s.status_counts.get('completed', 0)}",
             f"provider_error {s.status_counts.get('provider_error', 0)}",
             f"in_progress {s.in_progress}"]
    if s.unsupported:
        parts.append(f"unsupported (stream=True) {s.unsupported}")
    if s.start_missing:
        parts.append(f"start record missing {s.start_missing}")
    L.append("  " + "      ".join(parts))
    L.append("")
    dedup = ("deduplicated (exact identity only)" if s.inferred_unresolved == 0
             else f"possible over-count from {s.inferred_unresolved} unresolved pairs")
    L.append(
        f"KNOWN ESTIMATED COST   {format_usd(s.known_cost_nanos)}   list-price estimate, not actual spend   [{dedup}]"
    )
    priced_only = format_usd(s.known_cost_nanos - s.partial_cost_nanos)
    L.append(f"  priced          {s.est_counts.get('priced', 0):>5} charges   {priced_only}")
    if s.est_counts.get("partial"):
        L.append(
            f"  partial         {s.est_counts['partial']:>5} charges   {format_usd(s.partial_cost_nanos)}"
            "   (lower bound: some usage fields missing)"
        )
    unpriced = s.est_counts.get("unpriced", 0)
    if unpriced:
        detail = ", ".join(f'"{m}" ({n})' for m, n in sorted(s.unpriced_models.items()))
        L.append(f"  unpriced        {unpriced:>5} charges   model not in rate card: {detail}")
    else:
        L.append(f"  unpriced        {0:>5} charges")
    nu = s.est_counts.get("no_usage", 0)
    nu_detail = []
    if s.status_counts.get("provider_error"):
        nu_detail.append(f"{s.status_counts['provider_error']} errors")
    if s.in_progress:
        nu_detail.append(f"{s.in_progress} in progress")
    if s.unsupported:
        nu_detail.append(f"{s.unsupported} unsupported")
    L.append(f"  no_usage        {nu:>5} charges   " + (", ".join(nu_detail) if nu_detail else ""))
    if s.fallback_priced:
        L.append(f"  fallback rates  {s.fallback_priced:>5} charges priced on a card not effective at execution time")
    b = s.billing_counts
    L.append(f"  billing         expected {b.get('expected', 0)} · unknown {b.get('unknown', 0)}"
             f" · confirmed {b.get('confirmed', 0)} · not_billed {b.get('not_billed', 0)}")
    L.append("")
    completed = s.status_counts.get("completed", 0)
    priced_all = s.est_counts.get("priced", 0) + s.est_counts.get("partial", 0)
    L.append(f"COVERAGE          usage: exact {s.usage_exact} / missing {s.usage_missing} of {completed} completed"
             f"   ·   pricing: {priced_all} of {s.with_usage} with usage priced"
             f"   ·   capture: not measurable")
    ac = s.attribution_cost
    tot = s.known_cost_nanos
    L.append(
        f"ATTRIBUTION       by cost: attributed {_pct(ac.get('attributed', 0), tot)}"
        f" · job_only {_pct(ac.get('job_only', 0), tot)}"
        f" · unattributed {_pct(ac.get('unattributed', 0), tot)} ({format_usd(ac.get('unattributed', 0))})"
    )
    if s.conflicting:
        L.append(f"CORRELATION       usage conflicts between sources: {s.conflicting} (precedence applied, both kept)")
    L.append("")
    L.append(f"{'BY CUSTOMER':<26}{'ops':>5}  {'cost':>12}  {'share':>6}  unmeasured")
    for c in s.by_customer:
        measured = c["ops"] - c["unmeasured"]
        # A customer with no priced charge has an unknown cost, not a zero one.
        cost = format_usd(c["cost"]) if measured else "—"
        share = f"{c['share'] * 100:>5.1f}%" if measured else "     —"
        L.append(f"  {c['customer']:<24}{c['ops']:>5}  {cost:>12}  {share}  {c['unmeasured'] or ''}")
    L.append("")
    L.append(f"{'BY MODEL':<26}{'ops':>5}  {'in tok':>9}  {'out tok':>9}  {'cached':>8}  {'cost':>12}")
    for m in s.by_model:
        cost = format_usd(m["cost"]) if m["priced"] else ("unpriced" if m["unpriced"] else "no usage")
        L.append(f"  {m['model']:<24}{m['ops']:>5}  {m['in']:>9,}  {m['out']:>9,}  {m['cached']:>8,}  {cost:>12}")
    L.append("")
    c = s.counters
    if c:
        clean = bool(run.get("ended_at") and run.get("clean_exit"))
        if clean:
            L.append(
                f"TELEMETRY         records {c.get('records_written', 0)} · dropped {c.get('dropped')}"
                f" · unflushed at exit {c.get('unflushed')} · persist failures {c.get('persist_failures')}"
                f" · hook failures {s.hook_failures}"
            )
        else:
            # The run's own counters are written at clean shutdown; after a
            # crash only what reached the store can be counted.
            L.append(
                f"TELEMETRY         persisted rows {s.persisted_rows} (counted from the store)"
                " · final counters unavailable: dropped / unflushed / persist failures unknown"
                f" · hook failures persisted {s.hook_failures} (may be incomplete)"
            )
    if s.attempts == 0:
        L.append("")
        L.append("No AI operations were observed. This does not mean no AI spend occurred —")
        L.append("see the adapters line above for what was and was not instrumented.")
    return "\n".join(L)
