"""Local OpenTelemetry receiver for coding agents' documented telemetry.

Verified 2026-09-11 with real sessions (see docs/EVIDENCE_MATRIX.md):

* Claude Code 2.1.269 exports OTLP/HTTP JSON log records. ``claude_code.api_request``
  carries ``model``, ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
  ``cache_creation_tokens``, ``cost_usd_micros``, ``request_id``, ``session.id``,
  ``prompt.id`` and ``query_source``. The same record also carries ``user.email``,
  ``user.account_uuid``, ``user.account_id``, ``organization.id`` and ``user.id``
  which are never persisted here.
* Codex CLI 0.154.0 exports ``codex.sse_event`` with ``event.kind`` =
  ``response.completed`` carrying ``input_token_count`` (includes cached),
  ``cached_token_count``, ``cache_write_token_count``, ``output_token_count``,
  ``reasoning_token_count``, ``model`` and ``conversation.id`` (the rollout file's
  thread id). No request/response id is exported, and ``user.email`` /
  ``user.account_id`` are present and dropped.

The receiver binds loopback only, requires a bearer token, accepts only
``application/json`` bodies below a size bound, ignores metrics and traces, and
stores allowlisted fields only. It never logs request bodies.

Overlap with transcript adapters:

* Claude Code: telemetry and transcript describe the same request through
  ``request_id``. One usage row per request; both observers are recorded.
* Codex: no shared request identity. When telemetry has observed a session, the
  transcript rows for that session are quarantined (excluded from priced
  subtotals, counted in coverage) rather than double counted or fuzzily matched.
"""

from __future__ import annotations

import gzip
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .agents.ingest import CALC_VERSION
from .ids import now_iso
from .money import tokens_cost_nanos
from .rates import RateCardSet

MAX_BODY = 4 * 1024 * 1024
DEFAULT_PORT = 4327
PROVENANCE_TELEMETRY = "telemetry"
PROVENANCE_BOTH = "transcript+telemetry"
STATUS_QUARANTINED = "quarantined"

# Everything read from a log record. Anything else in the record is dropped unread.
CLAUDE_FIELDS = ("model", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                 "cost_usd_micros", "request_id", "session.id", "prompt.id", "query_source", "event.timestamp",
                 "event.name", "error", "status_code")
CODEX_FIELDS = ("event.name", "event.kind", "model", "input_token_count", "output_token_count", "cached_token_count",
                "cache_write_token_count", "reasoning_token_count", "conversation.id", "event.timestamp")


def _attrs(items: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in items or []:
        if not isinstance(a, dict) or not isinstance(a.get("key"), str):
            continue
        v = a.get("value")
        if isinstance(v, dict):
            for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if k in v:
                    out[a["key"]] = v[k]
                    break
    return out


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    return None


def _iso(v: Any, fallback_ns: Any) -> str | None:
    if isinstance(v, str) and v:
        return v
    ns = _int(fallback_ns)
    if ns:
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00",
                                                                                                             "Z")
    return None


def normalise(record: dict[str, Any], resource: dict[str, Any]) -> dict[str, Any] | None:
    """One allowlisted usage observation from a log record, or None if it is not a usage event."""
    a = _attrs(record.get("attributes"))
    name = a.get("event.name") or (record.get("body") or {}).get("stringValue")
    service = str(resource.get("service.name") or "")
    if name == "claude_code.api_request" or (name == "api_request" and service == "claude-code"):
        f = {k: a.get(k) for k in CLAUDE_FIELDS}
        sid, rid = f.get("session.id"), f.get("request_id")
        if not isinstance(sid, str) or not isinstance(rid, str):
            return None
        micros = _int(f.get("cost_usd_micros"))
        return {
            "source": "claude-code", "provider": "anthropic", "session_id": sid, "request_id": rid,
            "usage_id": f"otel:{rid}", "model": f.get("model") if isinstance(f.get("model"), str) else None,
            "at": _iso(f.get("event.timestamp"), record.get("timeUnixNano")),
            "input_tokens": _int(f.get("input_tokens")), "cache_read_tokens": _int(f.get("cache_read_tokens")),
            "cache_write_5m_tokens": _int(f.get("cache_creation_tokens")), "cache_write_1h_tokens": None,
            "output_tokens": _int(f.get("output_tokens")), "reasoning_tokens": None,
            "source_cost_nanos": micros * 1000 if micros is not None else None,
            "turn_id": f.get("prompt.id") if isinstance(f.get("prompt.id"), str) else None,
            "query_source": f.get("query_source") if isinstance(f.get("query_source"), str) else None,
            "source_version": str(resource.get("service.version") or "") or None,
        }
    if name == "codex.sse_event" and a.get("event.kind") == "response.completed":
        f = {k: a.get(k) for k in CODEX_FIELDS}
        cid = f.get("conversation.id")
        if not isinstance(cid, str):
            return None
        inp, cached = _int(f.get("input_token_count")) or 0, _int(f.get("cached_token_count")) or 0
        at = _iso(f.get("event.timestamp"), record.get("timeUnixNano"))
        counts = (inp, cached, _int(f.get("output_token_count")) or 0, _int(f.get("reasoning_token_count")) or 0,
                  _int(f.get("cache_write_token_count")) or 0)
        return {
            "source": "codex", "provider": "openai", "session_id": cid, "request_id": None,
            "usage_id": f"otel:{cid}:{at}:{':'.join(str(c) for c in counts)}",
            "model": f.get("model") if isinstance(f.get("model"), str) else None, "at": at,
            "input_tokens": max(inp - cached, 0), "cache_read_tokens": cached,
            "cache_write_5m_tokens": counts[4] or None, "cache_write_1h_tokens": None,
            "output_tokens": counts[2], "reasoning_tokens": counts[3], "source_cost_nanos": None,
            "turn_id": None, "query_source": None,
            "source_version": str(resource.get("service.version") or "") or None,
        }
    return None


def parse_logs(body: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rl in body.get("resourceLogs") or []:
        if not isinstance(rl, dict):
            continue
        resource = _attrs((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs") or []:
            if not isinstance(sl, dict):
                continue
            for rec in sl.get("logRecords") or []:
                if isinstance(rec, dict):
                    ev = normalise(rec, resource)
                    if ev:
                        out.append(ev)
    return out


# --------------------------------------------------------------------------- persistence


def _price(cards: RateCardSet, ev: dict[str, Any]) -> tuple[int | None, str, str | None, str | None]:
    at = None
    if ev.get("at"):
        try:
            at = datetime.fromisoformat(str(ev["at"]).replace("Z", "+00:00"))
        except ValueError:
            at = None
    card, resolution = cards.resolve(provider=ev["provider"], source="list", at=at, fallback="nearest_earlier",
                                     pin=None)
    if card is None:
        return None, "unpriced", None, None
    _, prices = card.prices_for(ev.get("model"))
    if prices is None:
        return None, "unpriced", card.rate_card_id, resolution
    amount = (tokens_cost_nanos(ev.get("input_tokens") or 0, prices.input)
              + tokens_cost_nanos(ev.get("cache_write_5m_tokens") or 0, prices.cache_write_5m or prices.input)
              + tokens_cost_nanos(ev.get("cache_read_tokens") or 0, prices.cached_input)
              + tokens_cost_nanos(ev.get("output_tokens") or 0, prices.output))
    return amount, "priced", card.rate_card_id, resolution


def store_events(conn: sqlite3.Connection, events: list[dict[str, Any]], *, cards: RateCardSet | None = None,
                 received_at: str | None = None) -> dict[str, int]:
    """Persist allowlisted usage observations. Idempotent per usage id and per request id."""
    cards = cards or RateCardSet.builtin()
    received_at = received_at or now_iso()
    stats = {"inserted": 0, "merged": 0, "duplicate": 0, "quarantined": 0}
    conn.execute("BEGIN")
    try:
        for ev in events:
            sid = ev["session_id"]
            conn.execute(
                "INSERT OR IGNORE INTO agent_sessions (session_id, source, source_version, project_path,"
                " transcript_path, first_seen_at, provider) VALUES (?,?,?,NULL,?,?,?)",
                (sid, ev["source"], ev.get("source_version"), f"telemetry://{sid}", received_at, ev["provider"]))
            conn.execute("UPDATE agent_sessions SET last_ingested_at = ?, telemetry_last_at = ? WHERE session_id = ?",
                         (received_at, ev.get("at") or received_at, sid))
            if ev.get("request_id"):
                existing = conn.execute(
                    "SELECT usage_id, provenance FROM agent_usage WHERE session_id = ? AND request_id = ?",
                    (sid, ev["request_id"])).fetchone()
                if existing is not None:
                    if existing["provenance"] in (PROVENANCE_TELEMETRY, PROVENANCE_BOTH):
                        stats["duplicate"] += 1
                    else:
                        conn.execute(
                            "UPDATE agent_usage SET source_cost_nanos = COALESCE(?, source_cost_nanos),"
                            " provenance = ?, telemetry_at = ? WHERE usage_id = ?",
                            (ev.get("source_cost_nanos"), PROVENANCE_BOTH, received_at, existing["usage_id"]))
                        stats["merged"] += 1
                    continue
            if conn.execute("SELECT 1 FROM agent_usage WHERE usage_id = ?", (ev["usage_id"],)).fetchone():
                stats["duplicate"] += 1
                continue
            amount, status, card_id, resolution = _price(cards, ev)
            conn.execute(
                "INSERT INTO agent_usage (usage_id, session_id, turn_id, request_id, model, at, input_tokens,"
                " cache_write_5m_tokens, cache_write_1h_tokens, cache_read_tokens, output_tokens, web_search_requests,"
                " web_fetch_requests, usage_kind, provenance, source_cost_nanos, api_equiv_nanos, api_equiv_status,"
                " rate_card_id, rate_resolution, calc_version, provider, reasoning_tokens, ordinal, telemetry_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'per_request',?,?,?,?,?,?,?,?,?,NULL,?)",
                (ev["usage_id"], sid, ev.get("turn_id"), ev.get("request_id"), ev.get("model"), ev.get("at"),
                 ev.get("input_tokens"), ev.get("cache_write_5m_tokens"), ev.get("cache_write_1h_tokens"),
                 ev.get("cache_read_tokens"), ev.get("output_tokens"), PROVENANCE_TELEMETRY,
                 ev.get("source_cost_nanos"), amount, status, card_id, resolution, CALC_VERSION, ev["provider"],
                 ev.get("reasoning_tokens"), received_at))
            stats["inserted"] += 1
            if ev["source"] == "codex":
                # Telemetry is authoritative for this session; transcript rows without a shared request
                # identity are quarantined instead of being counted twice or matched by timing.
                cur = conn.execute(
                    "UPDATE agent_usage SET api_equiv_status = ?, quarantine_reason = 'telemetry_authoritative'"
                    " WHERE session_id = ? AND provenance = 'provider_reported' AND api_equiv_status != ?",
                    (STATUS_QUARANTINED, sid, STATUS_QUARANTINED))
                stats["quarantined"] += cur.rowcount if cur.rowcount > 0 else 0
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stats


def quarantine_transcript_row(conn: sqlite3.Connection, session_id: str, usage_id: str) -> bool:
    """Called by the transcript ingestor for Codex rows: if telemetry already covers the session,
    the new transcript row is quarantined on insert."""
    row = conn.execute("SELECT 1 FROM agent_usage WHERE session_id = ? AND provenance IN (?, ?) LIMIT 1",
                       (session_id, PROVENANCE_TELEMETRY, PROVENANCE_BOTH)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE agent_usage SET api_equiv_status = ?, quarantine_reason = 'telemetry_authoritative'"
                 " WHERE usage_id = ?", (STATUS_QUARANTINED, usage_id))
    return True


# --------------------------------------------------------------------------- token + server


def ensure_token(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'telemetry_token'").fetchone()
    if row:
        return str(row[0])
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('telemetry_token', ?)", (token,))
    conn.commit()
    row = conn.execute("SELECT value FROM meta WHERE key = 'telemetry_token'").fetchone()
    return str(row[0])


class Receiver:
    """Loopback OTLP/HTTP receiver. One thread; SQLite writes are short transactions."""

    def __init__(self, conn_factory: Any, token: str, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 cards: RateCardSet | None = None) -> None:
        self.token = token
        self.cards = cards or RateCardSet.builtin()
        self.conn_factory = conn_factory
        self.stats = {"requests": 0, "rejected_auth": 0, "rejected_size": 0, "rejected_type": 0, "malformed": 0,
                      "inserted": 0, "merged": 0, "duplicate": 0, "quarantined": 0, "ignored_signals": 0}
        self._lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "runpeek"
            sys_version = ""

            def log_message(self, *_a: Any) -> None:  # never log bodies, tokens or paths
                pass

            def _reply(self, code: int, body: bytes = b"{}") -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self._reply(200, b'{"ok":true}')
                else:
                    self._reply(404)

            def do_POST(self) -> None:
                with receiver._lock:
                    receiver.stats["requests"] += 1
                auth = self.headers.get("Authorization", "")
                if not secrets.compare_digest(auth, "Bearer " + receiver.token):
                    with receiver._lock:
                        receiver.stats["rejected_auth"] += 1
                    self._reply(401, b'{"error":"unauthorized"}')
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY:
                    remaining = max(length, 0)
                    while remaining > 0:  # drain so the client sees the status instead of a broken pipe
                        chunk = self.rfile.read(min(remaining, 65536))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                    with receiver._lock:
                        receiver.stats["rejected_size"] += 1
                    self._reply(413, b'{"error":"payload too large"}')
                    return
                raw = self.rfile.read(length)
                if self.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except (OSError, EOFError):
                        raw = b""
                    if len(raw) > MAX_BODY:
                        with receiver._lock:
                            receiver.stats["rejected_size"] += 1
                        self._reply(413, b'{"error":"payload too large"}')
                        return
                if self.path != "/v1/logs":
                    with receiver._lock:
                        receiver.stats["ignored_signals"] += 1
                    self._reply(200)  # metrics/traces are accepted and discarded, never stored
                    return
                if "application/json" not in self.headers.get("Content-Type", ""):
                    with receiver._lock:
                        receiver.stats["rejected_type"] += 1
                    self._reply(415, b'{"error":"json only; set OTEL_EXPORTER_OTLP_PROTOCOL=http/json"}')
                    return
                try:
                    body = json.loads(raw)
                    events = parse_logs(body) if isinstance(body, dict) else []
                except (ValueError, RecursionError):
                    with receiver._lock:
                        receiver.stats["malformed"] += 1
                    self._reply(400, b'{"error":"malformed"}')
                    return
                if events:
                    conn = receiver.conn_factory()
                    try:
                        result = store_events(conn, events, cards=receiver.cards)
                    finally:
                        conn.close()
                    with receiver._lock:
                        for k, v in result.items():
                            receiver.stats[k] += v
                self._reply(200)

        self.server = HTTPServer((host, port), Handler)
        self.host, self.port = self.server.server_address[0], self.server.server_address[1]

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------- health, service


def receiver_port(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'telemetry_port'").fetchone()
    try:
        return int(row[0]) if row else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def set_receiver_port(conn: sqlite3.Connection, port: int) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES ('telemetry_port', ?) ON CONFLICT(key) DO UPDATE SET"
                 " value = excluded.value", (str(int(port)),))
    conn.commit()


def receiver_alive(conn: sqlite3.Connection, *, timeout: float = 0.5) -> bool:
    import urllib.request

    port = receiver_port(conn)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:  # noqa: S310
            return bool(r.status == 200)
    except (OSError, ValueError):
        return False


def serve(db_path: Any, *, port: int = DEFAULT_PORT, once_started: Any = None) -> None:
    """Run the receiver in the foreground until interrupted."""
    from .store import apply_schema, open_connection

    conn = open_connection(db_path)
    apply_schema(conn)
    token = ensure_token(conn)
    set_receiver_port(conn, port)
    conn.close()

    def factory() -> sqlite3.Connection:
        c = open_connection(db_path)
        apply_schema(c)
        return c

    rx = Receiver(factory, token, port=port)
    if once_started:
        once_started(rx)
    try:
        rx.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        rx.shutdown()


LAUNCHD_LABEL = "com.nemulai.runpeek.telemetry"


def launchd_plist_path() -> Any:
    from pathlib import Path

    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def install_service(command: list[str], db_path: Any, port: int) -> str:
    """macOS launchd user agent that keeps the receiver running across logins. Reversible."""
    import plistlib
    import subprocess
    import sys
    from pathlib import Path

    if sys.platform != "darwin":
        raise RuntimeError("background service install is implemented for macOS launchd only; run"
                           " `runpeek telemetry serve` in a terminal or your own service manager")
    plist = launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    logdir = Path.home() / ".runpeek"
    logdir.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [*command, "telemetry", "serve", "--db", str(db_path), "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logdir / "telemetry.log"),
        "StandardErrorPath": str(logdir / "telemetry.log"),
    }
    with open(plist, "wb") as fh:
        plistlib.dump(data, fh)
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    r = subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl load failed: {(r.stderr or r.stdout).strip()[:200]}")
    return str(plist)


def uninstall_service() -> str:
    import subprocess

    plist = launchd_plist_path()
    if not plist.exists():
        return "not installed"
    subprocess.run(["launchctl", "unload", "-w", str(plist)], capture_output=True)
    plist.unlink()
    return "removed"
