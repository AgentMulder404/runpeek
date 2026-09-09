"""Local SQLite store with a bounded queue and one background writer.

Hot-path contract: ``emit()`` does a dictionary build and ``put_nowait``.
Nothing else. If the queue is full the record is dropped, counted, and the
application continues.

Loss classes (see docs/DESIGN.md §5):
  dropped_confirmed  put_nowait raised Full
  unflushed_known    still queued or in-flight when the shutdown deadline hit
  persist_failures   a batch the writer could not commit
Unknown loss after a crash is inferred later from runs.heartbeat_at; this
module never claims an exact crash-loss count.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .ids import now_iso

Record = tuple[str, dict[str, Any]]

_STDERR_INTERVAL_S = 60.0


def _stderr(msg: str) -> None:
    try:
        sys.stderr.write(f"nemulai: {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def open_connection(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    sql = (resources.files("nemulai") / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    # Additive columns for stores created by earlier dev builds. Forward-only.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "app_exit_status" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN app_exit_status INTEGER")


def _j(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- SQL per record kind


def _apply(conn: sqlite3.Connection, kind: str, p: dict[str, Any]) -> None:
    if kind == "span_start":
        conn.execute(
            "INSERT INTO spans (span_id, run_id, parent_span_id, kind, name, customer_id, job_name, job_id,"
            " parent_job_id, attributes, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(span_id) DO UPDATE SET started_at = COALESCE(spans.started_at, excluded.started_at)",
            (
                p["span_id"], p["run_id"], p.get("parent_span_id"), p.get("kind", "job"), p.get("name"),
                p.get("customer_id"), p.get("job_name"), p.get("job_id"), p.get("parent_job_id"),
                _j(p.get("attributes")), p.get("started_at"),
            ),
        )
    elif kind == "span_end":
        conn.execute(
            "INSERT INTO spans (span_id, run_id, kind, ended_at, status) VALUES (?,?,'job',?,?)"
            " ON CONFLICT(span_id) DO UPDATE SET ended_at = excluded.ended_at, status = excluded.status",
            (p["span_id"], p["run_id"], p.get("ended_at"), p.get("status")),
        )
    elif kind == "operation":
        conn.execute(
            "INSERT OR IGNORE INTO operations (operation_id, run_id, span_id, surface, supported, started_at,"
            " customer_id, job_name, job_id, attribution_state) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                p["operation_id"], p["run_id"], p.get("span_id"), p["surface"], 1 if p.get("supported", True) else 0,
                p.get("started_at"), p.get("customer_id"), p.get("job_name"), p.get("job_id"),
                p["attribution_state"],
            ),
        )
    elif kind == "attempt_start":
        conn.execute(
            "INSERT INTO attempts (attempt_id, operation_id, run_id, visibility, provider, model_requested,"
            " started_at, observed_status) VALUES (?,?,?,?,?,?,?,'in_progress')"
            " ON CONFLICT(attempt_id) DO UPDATE SET"
            "   started_at = COALESCE(attempts.started_at, excluded.started_at),"
            "   model_requested = COALESCE(attempts.model_requested, excluded.model_requested),"
            "   start_missing = 0",
            (
                p["attempt_id"], p["operation_id"], p["run_id"], p.get("visibility", "last_only"),
                p["provider"], p.get("model_requested"), p.get("started_at"),
            ),
        )
    elif kind == "attempt_end":
        # A terminal without a start still creates the attempt (start_missing = 1).
        conn.execute(
            "INSERT INTO attempts (attempt_id, operation_id, run_id, visibility, provider, model_requested,"
            " model_served, ended_at, latency_ms, observed_status, error_class, http_status, start_missing)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)"
            " ON CONFLICT(attempt_id) DO UPDATE SET"
            "   model_served = excluded.model_served, ended_at = excluded.ended_at,"
            "   latency_ms = excluded.latency_ms, observed_status = excluded.observed_status,"
            "   error_class = excluded.error_class, http_status = excluded.http_status,"
            "   model_requested = COALESCE(attempts.model_requested, excluded.model_requested),"
            "   start_missing = CASE WHEN attempts.started_at IS NULL THEN 1 ELSE 0 END",
            (
                p["attempt_id"], p["operation_id"], p["run_id"], p.get("visibility", "last_only"),
                p["provider"], p.get("model_requested"), p.get("model_served"), p.get("ended_at"),
                p.get("latency_ms"), p["observed_status"], p.get("error_class"), p.get("http_status"),
            ),
        )
    elif kind == "observation":
        conn.execute(
            "INSERT OR IGNORE INTO source_observations (observation_id, attempt_id, source, collected_at,"
            " model_served, input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, usage_source,"
            " http_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                p["observation_id"], p["attempt_id"], p["source"], p["collected_at"], p.get("model_served"),
                p.get("input_tokens"), p.get("cached_input_tokens"), p.get("output_tokens"),
                p.get("reasoning_tokens"), p["usage_source"], p.get("http_status"),
            ),
        )
    elif kind == "identifier":
        conn.execute(
            "INSERT OR IGNORE INTO identifiers (id_kind, namespace, value, subject_kind, subject_id, source)"
            " VALUES (?,?,?,?,?,?)",
            (p["id_kind"], p["namespace"], p["value"], p["subject_kind"], p["subject_id"], p["source"]),
        )
    elif kind == "health":
        conn.execute(
            "INSERT INTO health_events (run_id, at, kind, detail) VALUES (?,?,?,?)",
            (p["run_id"], p.get("at") or now_iso(), p["kind"], _j(p.get("detail"))),
        )
    else:
        raise ValueError(f"unknown record kind {kind!r}")


# --------------------------------------------------------------------------- store


class SQLiteStore:
    def __init__(
        self,
        path: str | Path,
        run_id: str,
        *,
        command: str | None = None,
        queue_size: int = 10_000,
        batch_size: int = 500,
        batch_ms: int = 250,
        heartbeat_s: float = 10.0,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.command = command
        self._q: queue.Queue[Record | None] = queue.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._batch_s = batch_ms / 1000.0
        self._heartbeat_s = heartbeat_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._closed = False
        # counters — plain ints, read by close() and tests
        self.dropped_confirmed = 0
        self.persist_failures = 0
        self.records_written = 0
        self.unflushed_known: int | None = None
        self._inflight = 0
        self._sentinel_queued = False
        self._sentinel_consumed = False
        self._last_stderr: dict[str, float] = {}
        self._lock = threading.Lock()
        # test hook: when set, the writer parks before committing each batch
        self._pause: threading.Event | None = None

    # ----- lifecycle

    def start(self) -> None:
        if self._started:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        existed = self.path.exists()
        conn = open_connection(self.path)
        try:
            apply_schema(conn)
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, started_at, command, pid, harness_version)"
                " VALUES (?,?,?,?,?)",
                (self.run_id, now_iso(), self.command, os.getpid(), __version__),
            )
        finally:
            conn.close()
        if not existed:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._thread = threading.Thread(target=self._writer, name="nemulai-writer", daemon=True)
        self._thread.start()
        self._started = True

    def emit(self, kind: str, payload: dict[str, Any]) -> bool:
        """Enqueue a record. Returns False when it was dropped."""
        if self._closed:
            return False
        payload.setdefault("run_id", self.run_id)
        try:
            self._q.put_nowait((kind, payload))
            return True
        except queue.Full:
            with self._lock:
                self.dropped_confirmed += 1
            self._rate_limited_stderr("drop", "queue full; dropping telemetry records (application unaffected)")
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything enqueued so far is committed. Test/CLI aid."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # unfinished_tasks is incremented at put() time and decremented only
            # after a record's batch has been committed or failed — no window.
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return False

    def close(self, deadline_s: float = 5.0) -> dict[str, int]:
        """Bounded shutdown: drain within the deadline, record final counters."""
        if self._closed:
            return self._counters()
        self._closed = True
        try:
            self._q.put_nowait(None)  # sentinel
            self._sentinel_queued = True
        except queue.Full:
            pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(deadline_s)
        pending = self._q.unfinished_tasks
        if self._sentinel_queued and not self._sentinel_consumed:
            pending -= 1
        self.unflushed_known = max(pending, 0)
        counters = self._counters()
        try:
            conn = open_connection(self.path)
            try:
                conn.execute(
                    "UPDATE runs SET ended_at = ?, clean_exit = 1, records_written = ?, dropped_confirmed = ?,"
                    " unflushed_known = ?, persist_failures = ? WHERE run_id = ?",
                    (
                        now_iso(), self.records_written, self.dropped_confirmed, self.unflushed_known,
                        self.persist_failures, self.run_id,
                    ),
                )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            _stderr(f"could not record final counters: {exc}; counters were {counters}")
        if counters["dropped_confirmed"] or counters["unflushed_known"] or counters["persist_failures"]:
            _stderr(
                "telemetry loss this run: dropped {dropped_confirmed}, unflushed at exit {unflushed_known},"
                " persist failures {persist_failures}".format(**counters)
            )
        return counters

    def _counters(self) -> dict[str, int]:
        return {
            "records_written": self.records_written,
            "dropped_confirmed": self.dropped_confirmed,
            "unflushed_known": self.unflushed_known or 0,
            "persist_failures": self.persist_failures,
        }

    # ----- writer thread

    def _writer(self) -> None:
        try:
            conn = open_connection(self.path)
        except sqlite3.Error as exc:
            _stderr(f"cannot open store {self.path}: {exc}; telemetry will be dropped")
            self._drain_into_failures()
            return
        last_heartbeat = time.monotonic()
        try:
            while True:
                batch, done = self._next_batch()
                if batch:
                    self._commit(conn, batch)
                if time.monotonic() - last_heartbeat >= self._heartbeat_s:
                    self._heartbeat(conn)
                    last_heartbeat = time.monotonic()
                if done:
                    break
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _next_batch(self) -> tuple[list[Record], bool]:
        batch: list[Record] = []
        deadline = time.monotonic() + self._batch_s
        done = False
        while len(batch) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._q.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                self._sentinel_consumed = True
                self._q.task_done()
                done = True
                break
            batch.append(item)
        if not batch and self._stop.is_set() and self._q.empty():
            done = True
        return batch, done

    def _commit(self, conn: sqlite3.Connection, batch: list[Record]) -> None:
        self._inflight = len(batch)
        try:
            if self._pause is not None:
                self._pause.wait()
            conn.execute("BEGIN")
            for kind, payload in batch:
                _apply(conn, kind, payload)
            conn.execute("COMMIT")
            self.records_written += len(batch)
        except Exception as exc:  # sqlite3.Error or a bad payload — never propagate
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            with self._lock:
                self.persist_failures += len(batch)
            self._rate_limited_stderr("persist", f"could not persist {len(batch)} records: {exc!r}")
        finally:
            self._inflight = 0
            for _ in batch:
                self._q.task_done()

    def _heartbeat(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                "UPDATE runs SET heartbeat_at = ?, records_written = ? WHERE run_id = ?",
                (now_iso(), self.records_written, self.run_id),
            )
        except sqlite3.Error:
            pass

    def _drain_into_failures(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            self._q.task_done()
            if item is None:
                self._sentinel_consumed = True
                return
            with self._lock:
                self.persist_failures += 1

    def _rate_limited_stderr(self, key: str, msg: str) -> None:
        now = time.monotonic()
        last = self._last_stderr.get(key, -1e9)
        if now - last >= _STDERR_INTERVAL_S:
            self._last_stderr[key] = now
            _stderr(msg)


Emitter = Callable[[str, dict[str, Any]], bool]
