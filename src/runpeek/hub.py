"""Self-hosted tenant-scoped receiver. Deploy behind TLS; no transcript endpoints."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import ledger
from .store import open_connection

HUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_devices (
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
 role TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS hub_owned_charges (workspace TEXT NOT NULL, device TEXT NOT NULL, charge_key TEXT NOT NULL,
 PRIMARY KEY(workspace,device,charge_key));
CREATE TABLE IF NOT EXISTS hub_pairs (
 secret_hash TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, expires REAL NOT NULL,
 workspace TEXT, consumed INTEGER NOT NULL DEFAULT 0);
"""


def setup(conn: sqlite3.Connection) -> None:
    ledger.setup(conn)
    conn.executescript(HUB_SCHEMA)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def provision(conn: sqlite3.Connection, workspace: str, *, role: str = "collector") -> tuple[str, str]:
    if not ledger.IDENTIFIER.fullmatch(workspace) or role not in ("collector", "admin"):
        raise ValueError("Invalid workspace or role")
    device, token = secrets.token_hex(16), secrets.token_urlsafe(32)
    conn.execute("INSERT INTO hub_devices VALUES (?,?,?,?,?,0)",
                 (device, workspace, digest(token), role, time.time() + 90 * 86400))
    conn.execute("INSERT INTO ledger_audit(workspace,at,action,count) VALUES (?,?,?,1)",
                 (workspace, ledger.now(), "device_created"))
    return device, token


def approve(conn: sqlite3.Connection, code: str, workspace: str) -> None:
    if not ledger.IDENTIFIER.fullmatch(workspace):
        raise ValueError("Invalid workspace")
    cur = conn.execute("UPDATE hub_pairs SET workspace=? WHERE code=? AND expires>?"
                       " AND workspace IS NULL AND consumed=0", (workspace, code, time.time()))
    if cur.rowcount != 1:
        raise ValueError("Pairing code expired, already approved, or unknown")


def revoke(conn: sqlite3.Connection, workspace: str, device: str) -> None:
    conn.execute("UPDATE hub_devices SET revoked=1 WHERE workspace=? AND id=?", (workspace, device))
    conn.execute("INSERT INTO ledger_audit(workspace,at,action,count) VALUES (?,?,?,1)",
                 (workspace, ledger.now(), "device_revoked"))


class Application:
    def __init__(self, db: Path, *, quota: int = 1_000_000) -> None:
        self.db, self.quota = db, quota
        self._rates: dict[str, tuple[float, int]] = {}
        with open_connection(db) as conn:
            setup(conn)

    def limited(self, key: str, maximum: int) -> bool:
        current = time.monotonic()
        since, count = self._rates.get(key, (current, 0))
        if current - since >= 60:
            since, count = current, 0
        if len(self._rates) >= 10000 and key not in self._rates:
            self._rates = {k: v for k, v in self._rates.items() if current - v[0] < 60}
            if len(self._rates) >= 10000:
                return True
        self._rates[key] = since, count + 1
        return count >= maximum

    def __call__(self, env: dict[str, Any], start: Callable[..., Any]) -> Iterable[bytes]:
        status, data = 200, {}
        conn = None
        try:
            method, path = env.get("REQUEST_METHOD"), env.get("PATH_INFO")
            if self.limited("ip:" + str(env.get("REMOTE_ADDR", "unknown")), 120):
                return self.respond(start, 429, {"error": "Rate limit exceeded"})
            if method == "GET" and path == "/pair":
                return self.respond(start, 200, {"instructions": "Authorize the displayed device code on your hub: "
                                    "runpeek hub approve CODE --workspace WORKSPACE --db HUB_DB. "
                                    "Check the requesting device before approval. No data uploads before consent."})
            length = int(env.get("CONTENT_LENGTH") or 0)
            if length < 0 or length > ledger.MAX_BODY:
                return self.respond(start, 413, {"error": "Payload too large"})
            if method == "POST" and env.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                return self.respond(start, 415, {"error": "JSON required"})
            body = json.loads(env["wsgi.input"].read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
            conn = open_connection(self.db)
            if method == "POST" and path == "/v1/pair/start":
                if body:
                    raise ValueError("Unexpected pairing fields")
                if self.limited("pair:" + str(env.get("REMOTE_ADDR")), 5):
                    return self.respond(start, 429, {"error": "Pairing rate limit"})
                conn.execute("DELETE FROM hub_pairs WHERE expires < ?", (time.time(),))
                secret, code = secrets.token_urlsafe(32), secrets.token_hex(4).upper()
                conn.execute("INSERT INTO hub_pairs VALUES (?,?,?,NULL,0)",
                             (digest(secret), code, time.time() + 300))
                data = {"device_secret": secret, "user_code": code, "expires_in": 300, "interval": 5}
            elif method == "POST" and path == "/v1/pair/poll":
                if set(body) != {"device_secret"} or not isinstance(body["device_secret"], str):
                    raise ValueError("Pairing secret required")
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM hub_pairs WHERE secret_hash=? AND expires>? AND consumed=0",
                                   (digest(body["device_secret"]), time.time())).fetchone()
                if row is None:
                    raise ValueError("Pairing expired or consumed")
                if row["workspace"] is None:
                    data = {"pending": True}
                else:
                    device, token = provision(conn, row["workspace"])
                    conn.execute("UPDATE hub_pairs SET consumed=1 WHERE secret_hash=?", (row["secret_hash"],))
                    data = {"device_id": device, "token": token, "workspace": row["workspace"]}
                conn.execute("COMMIT")
            else:
                auth = env.get("HTTP_AUTHORIZATION", "")
                if not auth.startswith("Bearer ") or len(auth) > 256:
                    return self.respond(start, 401, {"error": "Authentication required"})
                principal = conn.execute("SELECT * FROM hub_devices WHERE token_hash=? AND revoked=0 AND expires>?",
                                         (digest(auth[7:]), time.time())).fetchone()
                if principal is None:
                    return self.respond(start, 401, {"error": "Invalid or revoked credential"})
                workspace = principal["workspace"]  # never from a request body or URL
                if self.limited("device:" + principal["id"], 60):
                    return self.respond(start, 429, {"error": "Device rate limit"})
                if method == "POST" and path == "/v1/events":
                    if set(body) != {"events"}:
                        raise ValueError("Only events accepted; workspace is assigned by authentication")
                    data = {"inserted": ledger.ingest(conn, workspace, body["events"], quota=self.quota)}
                    conn.executemany("INSERT OR IGNORE INTO hub_owned_charges VALUES (?,?,?)",
                        [(workspace, principal["id"], ledger.charge_key(ledger.validate(e))) for e in body["events"]])
                elif method == "POST" and path == "/v1/assign":
                    if set(body) != {"charge_key", "work_item_id"}:
                        raise ValueError("Charge and work ID required")
                    if not conn.execute("SELECT 1 FROM hub_owned_charges WHERE workspace=? AND device=?"
                                        " AND charge_key=?",
                                        (workspace, principal["id"], body["charge_key"])).fetchone():
                        return self.respond(start, 403, {"error": "Device must have observed the charge"})
                    ledger.assign(conn, workspace, body["charge_key"], body["work_item_id"])
                    data = {"assigned": True}
                elif method == "GET" and path == "/v1/report":
                    if principal["role"] != "admin":
                        return self.respond(start, 403, {"error": "Admin role required"})
                    data = ledger.report(conn, workspace)
                elif method == "POST" and path == "/v1/disconnect":
                    if body:
                        raise ValueError("Unexpected fields")
                    revoke(conn, workspace, principal["id"])
                    data = {"revoked": True}
                elif method == "POST" and path == "/v1/delete":
                    if principal["role"] != "admin":
                        return self.respond(start, 403, {"error": "Admin role required"})
                    if body != {"confirm": "delete-workspace"}:
                        raise ValueError("Explicit deletion confirmation required")
                    data = {"deleted_charges": ledger.delete(conn, workspace)}
                else:
                    status, data = 404, {"error": "Unknown endpoint"}
        except (ValueError, TypeError, KeyError):
            status, data = 400, {"error": "Invalid accounting request"}
        except Exception:
            status, data = 503, {"error": "Accounting service unavailable"}
        finally:
            if conn:
                conn.close()
        return self.respond(start, status, data)

    @staticmethod
    def respond(start: Callable[..., Any], status: int, data: dict[str, Any]) -> list[bytes]:
        from http import HTTPStatus
        payload = json.dumps(data).encode()
        start(f"{status} {HTTPStatus(status).phrase}", [("Content-Type", "application/json"),
              ("Content-Length", str(len(payload))), ("Cache-Control", "no-store"),
              ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "default-src 'none'"),
              ("Strict-Transport-Security", "max-age=31536000")])
        return [payload]
