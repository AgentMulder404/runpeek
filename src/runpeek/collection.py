"""Local adapter bridge. Only allowlisted metadata may enter the shared ledger."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

from . import ledger
from .agents import work
from .agents.ingest import fingerprint_key


def collect(conn: sqlite3.Connection) -> dict[str, int]:
    ledger.setup(conn)
    key = fingerprint_key(conn)

    def opaque(value: str) -> str:
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()

    assignments = work.assignment_map(conn)
    accepted = invalid = unassigned = 0
    pending: list[dict[str, object]] = []
    for row in conn.execute("SELECT u.*,s.source,s.parent_session_id FROM agent_usage u"
                            " JOIN agent_sessions s ON s.session_id=u.session_id").fetchall():
        assignment = assignments.get(row["session_id"])
        if assignment is None:
            unassigned += 1
            continue
        event = {"schema_version": 1, "event_id": opaque(row["usage_id"]), "source": row["source"],
                 "work_item_id": assignment[0], "agent": row["source"], "session_id": opaque(row["session_id"]),
                 "parent_session_id": opaque(row["parent_session_id"]) if row["parent_session_id"] else None,
                 "provider": row["provider"] or "unknown", "account_scope": "default",
                 "request_id": row["request_id"], "model": row["model"], "at": row["at"],
                 "basis": "estimated", "amount_nanos": row["api_equiv_nanos"], "rate_card": row["rate_card_id"],
                 "input_tokens": row["input_tokens"], "cache_read_tokens": row["cache_read_tokens"],
                 "cache_write_tokens": (row["cache_write_5m_tokens"] or 0) + (row["cache_write_1h_tokens"] or 0),
                 "output_tokens": row["output_tokens"], "reasoning_tokens": row["reasoning_tokens"]}
        try:
            clean = ledger.validate(event)
            old = conn.execute("SELECT payload FROM ledger_observations WHERE workspace='local'"
                               " AND source=? AND event_id=?", (clean["source"], clean["event_id"])).fetchone()
            if old:
                previous = json.loads(old[0])
                if dict(previous, work_item_id=clean["work_item_id"]) != clean:
                    invalid += 1
                    continue
                charge = ledger.charge_key(previous)
                override = conn.execute("SELECT work_item_id FROM ledger_assignments"
                                        " WHERE workspace='local' AND charge_key=?", (charge,)).fetchone()
                assigned = override[0] if override else previous["work_item_id"]
                if assigned != clean["work_item_id"]:
                    ledger.assign(conn, "local", charge, clean["work_item_id"])
                continue
            pending.append(clean)
            if len(pending) == ledger.MAX_BATCH:
                accepted += ledger.ingest(conn, "local", pending)
                pending = []
        except ValueError:
            invalid += 1
    if pending:
        accepted += ledger.ingest(conn, "local", pending)
    return {"inserted": accepted, "unassigned_usage": unassigned, "conflicting_or_invalid": invalid}
