"""Explicit, metadata-only sync with OS credential storage and durable receipts."""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import ledger


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def endpoint_url(endpoint: str) -> str:
    u = urlsplit(endpoint)
    if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
        raise ValueError("Use a server origin without credentials, query, or path")
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "::1", "localhost")):
        raise ValueError("Remote sync requires HTTPS; HTTP is allowed only on loopback")
    if not u.hostname:
        raise ValueError("Server hostname required")
    return endpoint.rstrip("/")


def request(endpoint: str, path: str, body: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    url = endpoint_url(endpoint) + path
    data = json.dumps(body).encode()
    if len(data) > ledger.MAX_BODY:
        raise ValueError("Sync payload exceeds limit")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    # Never forward credentials on redirects or through ambient HTTP proxies.
    opener = build_opener(NoRedirect(), ProxyHandler({}))
    req = Request(url, data=data, headers=headers, method="POST")
    with opener.open(req, timeout=10) as response:
        raw = response.read(ledger.MAX_BODY + 1)
        if len(raw) > ledger.MAX_BODY:
            raise ValueError("Server response exceeds limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid server response")
        return result


def credential(endpoint: str, value: str | None = None, *, remove: bool = False) -> str | None:
    try:
        keyring = importlib.import_module("keyring")
    except ImportError as exc:
        raise ValueError("Install runpeek[sync] to use the operating system credential store") from exc
    backend = keyring.get_keyring()
    # Refuse third-party plaintext fallback keyrings.
    if not type(backend).__module__.startswith(("keyring.backends.macOS", "keyring.backends.Windows",
                                              "keyring.backends.SecretService", "keyring.backends.kwallet")):
        raise ValueError("An OS-backed credential store is required; no plaintext fallback is allowed")
    account = hashlib.sha256(endpoint.encode()).hexdigest()
    if remove:
        if keyring.get_password("runpeek", account):
            keyring.delete_password("runpeek", account)
        return None
    if value is not None:
        keyring.set_password("runpeek", account, value)
        return None
    result: str | None = keyring.get_password("runpeek", account)
    return result


def setting(conn: sqlite3.Connection, key: str, value: str | None = None) -> str | None:
    if value is not None:
        conn.execute("INSERT INTO ledger_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))
        return value
    row = conn.execute("SELECT value FROM ledger_settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def push(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    ledger.setup(conn)
    endpoint = setting(conn, "endpoint")
    if not endpoint:
        raise ValueError("Connect a device first with `runpeek connect`")
    if setting(conn, "paused") == "true":
        return {"paused": True, "uploaded": 0}
    if not force and float(setting(conn, "retry_after") or 0) > time.time():
        return {"backoff": True, "uploaded": 0}
    token = credential(endpoint)
    if not token:
        raise ValueError("Credential unavailable; reconnect this device")
    uploaded = 0
    try:
        while True:
            rows = conn.execute(
                "SELECT o.source,o.event_id,o.payload FROM ledger_observations o LEFT JOIN ledger_outbox q"
                " ON q.endpoint=? AND q.source=o.source AND q.event_id=o.event_id"
                " WHERE o.workspace='local' AND q.event_id IS NULL LIMIT ?", (endpoint, ledger.MAX_BATCH)
            ).fetchall()
            if not rows:
                break
            result = request(endpoint, "/v1/events", {"events": [json.loads(r[2]) for r in rows]}, token)
            if type(result.get("inserted")) is not int:
                raise ValueError("Server did not acknowledge batch")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany("INSERT OR REPLACE INTO ledger_outbox VALUES (?,?,?,?)",
                                 [(endpoint, r[0], r[1], hashlib.sha256(r[2].encode()).hexdigest()) for r in rows])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            uploaded += len(rows)
        for row in conn.execute(
            "SELECT a.charge_key,a.work_item_id FROM ledger_assignments a LEFT JOIN ledger_assignment_receipts r"
            " ON r.endpoint=? AND r.charge_key=a.charge_key WHERE a.workspace='local'"
            " AND (r.work_item_id IS NULL OR r.work_item_id != a.work_item_id)", (endpoint,)
        ).fetchall():
            result = request(endpoint, "/v1/assign", {"charge_key": row[0], "work_item_id": row[1]}, token)
            if result.get("assigned") is not True:
                raise ValueError("Server did not acknowledge attribution")
            conn.execute("INSERT OR REPLACE INTO ledger_assignment_receipts VALUES (?,?,?)", (endpoint, row[0], row[1]))
        setting(conn, "failures", "0")
        setting(conn, "retry_after", "0")
        return {"uploaded": uploaded}
    except (OSError, ValueError, HTTPError):
        failures = min(int(setting(conn, "failures") or 0) + 1, 10)
        delay = min(2**failures, 3600)
        setting(conn, "failures", str(failures))
        setting(conn, "retry_after", str(time.time() + delay))
        return {"uploaded": uploaded, "retry_in_seconds": delay, "error": "Sync failed; observations retained locally"}
