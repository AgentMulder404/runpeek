"""Store-side accounting passes: charges, selection, exact correlation, estimates.

All passes are idempotent and re-runnable. They add rows; they never edit a
source observation and never delete anything. Running the same pass twice
with identical inputs changes nothing (estimate keys are content hashes).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .ids import REQUEST_IDENTITY_KINDS, new_id, now_iso
from .money import tokens_cost_nanos
from .perspective import Perspective, ensure
from .rates import RateCardSet

CALC_VERSION = 1

SOURCE_RANK: dict[str, int] = {
    "harness.openai": 0,
    "proxy": 1,
    "framework": 2,
    "otel": 3,
    "user": 4,
}
USAGE_RANK = {"exact": 0, "estimated": 1, "missing": 2}

# Usage disagreement beyond this is a conflict: max(1 %, 5 tokens).
CONFLICT_PCT = 0.01
CONFLICT_MIN_TOKENS = 5


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def source(self) -> str:
        return "missing" if self.input_tokens is None and self.output_tokens is None else "exact"


# --------------------------------------------------------------------------- charges


def ensure_charges(conn: sqlite3.Connection) -> int:
    """One potential charge per attempt. billing_status follows the evidence."""
    rows = conn.execute(
        "SELECT a.attempt_id, a.observed_status FROM attempts a"
        " LEFT JOIN charges c ON c.attempt_id = a.attempt_id WHERE c.charge_id IS NULL"
    ).fetchall()
    n = 0
    for r in rows:
        billing = _billing_status(conn, r["attempt_id"], r["observed_status"])
        conn.execute(
            "INSERT OR IGNORE INTO charges (charge_id, attempt_id, kind, billing_status, created_at)"
            " VALUES (?,?,?,?,?)",
            (new_id("chg"), r["attempt_id"], "provider", billing, now_iso()),
        )
        n += 1
    # in_progress attempts that later completed: refresh billing status
    conn.execute(
        "UPDATE charges SET billing_status = 'expected' WHERE billing_status = 'unknown' AND attempt_id IN"
        " (SELECT a.attempt_id FROM attempts a JOIN source_observations o ON o.attempt_id = a.attempt_id"
        "  WHERE a.observed_status = 'completed' AND o.usage_source != 'missing')"
    )
    return n


def _billing_status(conn: sqlite3.Connection, attempt_id: str, observed_status: str) -> str:
    if observed_status != "completed":
        return "unknown"
    has_usage = conn.execute(
        "SELECT 1 FROM source_observations WHERE attempt_id = ? AND usage_source != 'missing' LIMIT 1",
        (attempt_id,),
    ).fetchone()
    return "expected" if has_usage else "unknown"


# --------------------------------------------------------------------------- selection


def select_observations(conn: sqlite3.Connection) -> None:
    """Exactly one selected observation per attempt: best source rank, then
    best usage rank, then earliest collected. Selection is a flag, never a
    deletion; re-running keeps an existing selection unless a better source
    has since arrived."""
    attempts = conn.execute("SELECT DISTINCT attempt_id FROM source_observations").fetchall()
    for a in attempts:
        obs = conn.execute(
            "SELECT observation_id, source, usage_source, collected_at, selected FROM source_observations"
            " WHERE attempt_id = ?",
            (a["attempt_id"],),
        ).fetchall()
        best = min(
            obs,
            key=lambda o: (SOURCE_RANK.get(o["source"], 99), USAGE_RANK.get(o["usage_source"], 9), o["collected_at"]),
        )
        if best["selected"]:
            continue
        conn.execute("UPDATE source_observations SET selected = 0 WHERE attempt_id = ?", (a["attempt_id"],))
        conn.execute(
            "UPDATE source_observations SET selected = 1 WHERE observation_id = ?", (best["observation_id"],)
        )


# --------------------------------------------------------------------------- ingestion of other sources


def ingest_observation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source: str,
    provider: str,
    identifiers: list[tuple[str, str, str]],
    usage: Usage,
    model_served: str | None,
    collected_at: str | None = None,
    operation_id: str | None = None,
    http_status: int | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> str:
    """Add a source observation from a non-harness source (a proxy export, a
    framework callback). Returns the attempt id it attached to.

    Exact correlation requires a *request-identity* identifier already known
    for an attempt. A shared ``harness_operation_id`` only groups attempts
    under one operation; it never merges them.
    """
    collected_at = collected_at or now_iso()
    attempt_id: str | None = None
    for kind, ns, value in identifiers:
        if kind not in REQUEST_IDENTITY_KINDS:
            continue
        row = conn.execute(
            "SELECT subject_kind, subject_id FROM identifiers WHERE id_kind = ? AND namespace = ? AND value = ?",
            (kind, ns, value),
        ).fetchone()
        if row and row["subject_kind"] == "attempt":
            attempt_id = row["subject_id"]
            break

    if attempt_id is None:
        op_from_ids = next(
            (v for k, _ns, v in identifiers if k == "harness_operation_id"), operation_id
        )
        if op_from_ids and conn.execute(
            "SELECT 1 FROM operations WHERE operation_id = ?", (op_from_ids,)
        ).fetchone():
            operation_id = op_from_ids
        else:
            operation_id = new_id("op")
            conn.execute(
                "INSERT INTO operations (operation_id, run_id, surface, supported, started_at, attribution_state)"
                " VALUES (?,?,?,?,?, 'unattributed')",
                (operation_id, run_id, f"{source}.{provider}", 1, started_at or collected_at),
            )
        attempt_id = new_id("att")
        conn.execute(
            "INSERT INTO attempts (attempt_id, operation_id, run_id, visibility, provider, model_served,"
            " started_at, ended_at, observed_status, http_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id, operation_id, run_id, "single" if source == "proxy" else "aggregate", provider,
                model_served, started_at, ended_at or collected_at,
                "completed" if usage.source != "missing" else "provider_error", http_status,
            ),
        )
        for kind, ns, value in identifiers:
            conn.execute(
                "INSERT OR IGNORE INTO identifiers (id_kind, namespace, value, subject_kind, subject_id, source)"
                " VALUES (?,?,?,?,?,?)",
                (kind, ns, value, "attempt", attempt_id, source),
            )

    observation_id = new_id("obs")
    conn.execute(
        "INSERT INTO source_observations (observation_id, attempt_id, source, collected_at, model_served,"
        " input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, usage_source, http_status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id, attempt_id, source, collected_at, model_served, usage.input_tokens,
            usage.cached_input_tokens, usage.output_tokens, usage.reasoning_tokens, usage.source, http_status,
        ),
    )
    # identifiers this source brought that the attempt did not have yet
    for kind, ns, value in identifiers:
        conn.execute(
            "INSERT OR IGNORE INTO identifiers (id_kind, namespace, value, subject_kind, subject_id, source)"
            " VALUES (?,?,?,?,?,?)",
            (kind, ns, value, "attempt", attempt_id, source),
        )
    _correlate_within_attempt(conn, attempt_id, observation_id)
    return attempt_id


def _correlate_within_attempt(conn: sqlite3.Connection, attempt_id: str, new_obs: str) -> None:
    others = conn.execute(
        "SELECT observation_id, input_tokens, output_tokens, usage_source FROM source_observations"
        " WHERE attempt_id = ? AND observation_id != ?",
        (attempt_id, new_obs),
    ).fetchall()
    if not others:
        return
    me = conn.execute(
        "SELECT input_tokens, output_tokens, usage_source FROM source_observations WHERE observation_id = ?",
        (new_obs,),
    ).fetchone()
    for o in others:
        cls = "exact"
        if me["usage_source"] != "missing" and o["usage_source"] != "missing":
            if _differs(me["input_tokens"], o["input_tokens"]) or _differs(me["output_tokens"], o["output_tokens"]):
                cls = "conflicting"
        conn.execute(
            "INSERT INTO correlations (correlation_id, observation_a, observation_b, class, evidence, decided_by,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (new_id("cor"), o["observation_id"], new_obs, cls, "same_request_identity", "system", now_iso()),
        )


def _differs(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) > max(CONFLICT_MIN_TOKENS, int(max(a, b) * CONFLICT_PCT))


# --------------------------------------------------------------------------- estimates


def estimate_key(charge_id: str, perspective_id: str, obs_id: str | None, rate_card_id: str | None) -> str:
    h = hashlib.sha256(
        "|".join([charge_id, perspective_id, obs_id or "", rate_card_id or "", str(CALC_VERSION)]).encode()
    )
    return "est_" + h.hexdigest()[:32]


def estimate_all(conn: sqlite3.Connection, perspective: Perspective, cards: RateCardSet) -> int:
    """One current estimate per (charge, perspective). Returns rows created."""
    ensure(conn, perspective)
    rows = conn.execute(
        "SELECT c.charge_id, a.attempt_id, a.provider, a.started_at, a.model_served, a.model_requested,"
        "       o.observation_id, o.input_tokens, o.cached_input_tokens, o.output_tokens, o.usage_source"
        " FROM charges c JOIN attempts a ON a.attempt_id = c.attempt_id"
        " LEFT JOIN source_observations o ON o.attempt_id = a.attempt_id AND o.selected = 1"
    ).fetchall()
    created = 0
    for r in rows:
        created += _estimate_one(conn, perspective, cards, r)
    return created


def _estimate_one(conn: sqlite3.Connection, p: Perspective, cards: RateCardSet, r: sqlite3.Row) -> int:
    started = _parse_ts(r["started_at"])
    card, resolution = cards.resolve(
        provider=r["provider"], source=p.rates if p.rates != "reconciled" else "list",
        at=started, fallback=p.fallback, pin=p.pin,
    )
    key = estimate_key(r["charge_id"], p.perspective_id, r["observation_id"], card.rate_card_id if card else None)
    if conn.execute("SELECT 1 FROM cost_estimates WHERE estimate_key = ?", (key,)).fetchone():
        return 0

    usage_prov = r["usage_source"] or "missing"
    model = r["model_served"] or r["model_requested"]
    detail: dict[str, Any] = {"model": model}
    amount: int | None = None
    status: str
    completeness = "complete"
    model_key: str | None = None
    if usage_prov == "missing" or r["observation_id"] is None:
        status = "no_usage"
    elif card is None:
        status = "unpriced"
        detail["reason"] = "no rate card resolved"
    else:
        model_key, prices = card.prices_for(model)
        if prices is None:
            status = "unpriced"
            detail["reason"] = f"model {model!r} not in {card.rate_card_id}"
        else:
            detail["model_key"] = model_key
            inp, out = r["input_tokens"], r["output_tokens"]
            cached = r["cached_input_tokens"] or 0
            missing = [n for n, v in (("input_tokens", inp), ("output_tokens", out)) if v is None]
            if missing:
                # Price what is known; label the amount a lower bound.
                completeness = "partial"
                status = "partial"
                detail["missing"] = missing
                inp = inp or 0
                out = out or 0
            else:
                status = "priced"
            billable_input = max(inp - cached, 0)
            amount = (
                tokens_cost_nanos(billable_input, prices.input)
                + tokens_cost_nanos(cached, prices.cached_input)
                + tokens_cost_nanos(out, prices.output)
            )
            detail["billable_input"] = billable_input
            detail["cached_input"] = cached
    prev = conn.execute(
        "SELECT estimate_key, revision FROM cost_estimates WHERE charge_id = ? AND perspective_id = ? AND current = 1",
        (r["charge_id"], p.perspective_id),
    ).fetchone()
    revision = (prev["revision"] + 1) if prev else 1
    if prev:
        conn.execute(
            "UPDATE cost_estimates SET current = 0, superseded_by = ? WHERE estimate_key = ?",
            (key, prev["estimate_key"]),
        )
    conn.execute(
        "INSERT INTO cost_estimates (estimate_key, charge_id, perspective_id, revision, current, estimation_status,"
        " amount_nanos, currency, rate_card_id, rate_resolution, usage_provenance, rate_provenance, completeness,"
        " selected_observation_id, calc_version, detail, created_at)"
        " VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            key, r["charge_id"], p.perspective_id, revision, status, amount,
            card.currency if card else "USD", card.rate_card_id if card else None, resolution,
            usage_prov, (card.source if card else "none"), completeness, r["observation_id"], CALC_VERSION,
            json.dumps(detail, sort_keys=True), now_iso(),
        ),
    )
    return 1


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------- the pass


def run(conn: sqlite3.Connection, perspective: Perspective, cards: RateCardSet) -> dict[str, int]:
    """Charges → selection → estimates, in one transaction. Idempotent."""
    conn.execute("BEGIN")
    try:
        charges = ensure_charges(conn)
        select_observations(conn)
        estimates = estimate_all(conn, perspective, cards)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"charges_created": charges, "estimates_created": estimates}
