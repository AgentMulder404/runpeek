"""Metadata-only, tenant-scoped accounting. No model calls or fuzzy reconciliation."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_observations (
 workspace TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL,
 charge_key TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
 PRIMARY KEY(workspace,source,event_id));
CREATE INDEX IF NOT EXISTS ledger_charge ON ledger_observations(workspace,charge_key);
CREATE TABLE IF NOT EXISTS ledger_audit (
 id INTEGER PRIMARY KEY, workspace TEXT NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_assignments (workspace TEXT NOT NULL, charge_key TEXT NOT NULL,
 work_item_id TEXT NOT NULL, PRIMARY KEY(workspace,charge_key));
CREATE TABLE IF NOT EXISTS ledger_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_tombstones (workspace TEXT NOT NULL, charge_key TEXT NOT NULL,
 PRIMARY KEY(workspace,charge_key));
CREATE TABLE IF NOT EXISTS ledger_assignment_receipts (endpoint TEXT NOT NULL, charge_key TEXT NOT NULL,
 work_item_id TEXT NOT NULL, PRIMARY KEY(endpoint,charge_key));
CREATE TABLE IF NOT EXISTS ledger_outbox (
 endpoint TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(endpoint,source,event_id));
"""
TEXT_FIELDS = {"event_id", "source", "work_item_id", "agent", "session_id", "parent_session_id",
               "attempt_id", "provider", "account_scope", "request_id", "model", "at", "basis", "rate_card"}
TOKEN_FIELDS = {"input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"}
FIELDS = TEXT_FIELDS | TOKEN_FIELDS | {"schema_version", "amount_nanos"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,200}$")
BASES = {"estimated", "actual", "allocated"}
MAX_BATCH = 200
MAX_BODY = 256 * 1024


def setup(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError("Observation has unknown fields; only accounting metadata is accepted")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("Unsupported schema version")
    required = {"event_id", "source", "work_item_id", "agent", "session_id", "provider", "account_scope", "at", "basis"}
    if any(not raw.get(k) for k in required):
        raise ValueError("Missing required accounting metadata")
    for key in TEXT_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise ValueError(f"Invalid metadata identifier: {key}")
    try:
        at = datetime.fromisoformat(raw["at"].replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError("Timestamp must include timezone")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid timestamp") from exc
    if raw["basis"] not in BASES:
        raise ValueError("Unknown cost basis")
    for key in TOKEN_FIELDS | {"amount_nanos"}:
        value = raw.get(key)
        if value is not None and (type(value) is not int or value < 0 or value > 10**15):
            raise ValueError(f"Invalid bounded integer: {key}")
    if raw.get("reasoning_tokens", 0) and raw.get("output_tokens") is not None:
        if raw["reasoning_tokens"] > raw["output_tokens"]:
            raise ValueError("Reasoning tokens must be included in output tokens")
    return {k: v for k, v in raw.items() if v is not None}


def canonical(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def charge_key(event: dict[str, Any]) -> str:
    # Allocations must name a distinct source/event. They are not matched by
    # amount, timestamps, or guessed similarity to another provider's calls.
    if event.get("request_id"):
        parts = [event["provider"], event["account_scope"], event["request_id"]]
    else:
        parts = [event["source"], event["event_id"]]
    return hashlib.sha256(canonical({"identity": parts}).encode()).hexdigest()


def ingest(conn: sqlite3.Connection, workspace: str, events: list[dict[str, Any]], *, quota: int = 1_000_000) -> int:
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH:
        raise ValueError("Batch must contain 1–200 observations")
    clean = [validate(e) for e in events]
    if len(canonical({"events": clean}).encode()) > MAX_BODY:
        raise ValueError("Batch exceeds size limit")
    inserted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        count = conn.execute("SELECT COUNT(*) FROM ledger_observations WHERE workspace=?", (workspace,)).fetchone()[0]
        for e in clean:
            payload = canonical(e)
            key = charge_key(e)
            if conn.execute("SELECT 1 FROM ledger_tombstones WHERE workspace=? AND charge_key=?",
                            (workspace, key)).fetchone():
                continue  # acknowledge deleted events without resurrecting them
            old = conn.execute("SELECT payload FROM ledger_observations WHERE workspace=? AND source=? AND event_id=?",
                               (workspace, e["source"], e["event_id"])).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError("Event identity reused with different data")
                continue
            if count + inserted >= quota:
                raise ValueError("Workspace observation quota reached")
            conn.execute("INSERT INTO ledger_observations VALUES (?,?,?,?,?,?)",
                         (workspace, e["source"], e["event_id"], key, payload, now()))
            inserted += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return inserted


def export(conn: sqlite3.Connection, workspace: str) -> list[dict[str, Any]]:
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM ledger_observations WHERE workspace=? ORDER BY source,event_id", (workspace,))]


def report(conn: sqlite3.Connection, workspace: str, work_item: str | None = None) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute("SELECT charge_key,payload FROM ledger_observations WHERE workspace=?", (workspace,)):
        groups.setdefault(row[0], []).append(json.loads(row[1]))
    totals = dict.fromkeys(("actual", "estimated", "allocated"), 0)
    conflicts = unknown = priced = unmatched = 0
    sources: dict[str, str] = {}
    rank = {"actual": 3, "allocated": 2, "estimated": 1}
    overrides = {r[0]: r[1] for r in conn.execute(
        "SELECT charge_key,work_item_id FROM ledger_assignments WHERE workspace=?", (workspace,))}
    for key, events in groups.items():
        if key in overrides:
            events = [dict(e, work_item_id=overrides[key]) for e in events]
        if work_item and not any(e["work_item_id"] == work_item for e in events):
            continue
        for e in events:
            sources[e["source"]] = max(sources.get(e["source"], ""), e["at"])
        unmatched += int(not any(e.get("request_id") for e in events))
        if len({e["work_item_id"] for e in events}) > 1:
            conflicts += 1
            continue
        candidates = [e for e in events if e.get("amount_nanos") is not None]
        if not candidates:
            unknown += 1
            continue
        priority = max(rank[e["basis"]] for e in candidates)
        selected = [e for e in candidates if rank[e["basis"]] == priority]
        if len({e["amount_nanos"] for e in selected}) > 1:
            conflicts += 1
            continue
        winner = selected[0]
        totals[winner["basis"]] += winner["amount_nanos"]
        priced += 1
    return {"work_item_id": work_item, "accounted_nanos": sum(totals.values()), "by_basis_nanos": totals,
            "priced_charges": priced, "unknown_charges": unknown, "conflicting_charges": conflicts,
            "without_request_identity": unmatched, "sources_last_event_at": sources,
            "policy": "Actual replaces estimates for the same request; conflicts excluded. Allocations must cover "
                      "distinct activity. Completeness outside reporting sources is unknown.",
            "tracking_model_tokens": 0}


def delete(conn: sqlite3.Connection, workspace: str, before: str | None = None) -> int:
    # Delete whole charges, including conflicting observations, so remaining
    # evidence cannot unexpectedly change the accounted total.
    if before:
        datetime.fromisoformat(before.replace("Z", "+00:00"))
    rows = conn.execute("SELECT charge_key,MAX(received_at) FROM ledger_observations WHERE workspace=?"
                        " GROUP BY charge_key", (workspace,)).fetchall()
    keys = [r[0] for r in rows if before is None or r[1] < before]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for key in keys:
            conn.execute("INSERT OR IGNORE INTO ledger_tombstones VALUES (?,?)", (workspace, key))
            conn.execute("DELETE FROM ledger_observations WHERE workspace=? AND charge_key=?", (workspace, key))
            conn.execute("DELETE FROM ledger_assignments WHERE workspace=? AND charge_key=?", (workspace, key))
            conn.execute("DELETE FROM ledger_assignment_receipts WHERE charge_key=?", (key,))
        conn.execute("INSERT INTO ledger_audit(workspace,at,action,count) VALUES (?,?,?,?)",
                     (workspace, now(), "delete", len(keys)))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(keys)


def assign(conn: sqlite3.Connection, workspace: str, key: str, work_item_id: str) -> None:
    """Explicitly resolve work attribution without rewriting observation evidence."""
    if not IDENTIFIER.fullmatch(work_item_id):
        raise ValueError("Invalid work item ID")
    if not conn.execute("SELECT 1 FROM ledger_observations WHERE workspace=? AND charge_key=?",
                        (workspace, key)).fetchone():
        raise ValueError("Unknown charge")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO ledger_assignments VALUES (?,?,?) ON CONFLICT(workspace,charge_key)"
                     " DO UPDATE SET work_item_id=excluded.work_item_id", (workspace, key, work_item_id))
        conn.execute("INSERT INTO ledger_audit(workspace,at,action,count) VALUES (?,?,?,1)",
                     (workspace, now(), "charge_reassigned"))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def render(result: dict[str, Any]) -> str:
    from decimal import Decimal

    def usd(nanos: int) -> str:
        return "<$0.01" if 0 < nanos < 10_000_000 else f"${Decimal(nanos) / Decimal(1_000_000_000):,.2f}"

    lines = [f"ACCOUNTED COST  {usd(result['accounted_nanos'])}",
             "Work: " + (result["work_item_id"] or "all recorded work")]
    for basis, amount in result["by_basis_nanos"].items():
        lines.append(f"  {basis.capitalize():<10} {usd(amount)}")
    lines.extend([f"{result['priced_charges']} priced charges · {result['unknown_charges']} unknown · "
                  f"{result['conflicting_charges']} conflicting (excluded)",
                  f"{result['without_request_identity']} charges lack a cross-source request identity.",
                  "Sources: " + (", ".join(result["sources_last_event_at"]) or "none"),
                  result["policy"], "Tracking model tokens: 0"])
    return "\n".join(lines)
