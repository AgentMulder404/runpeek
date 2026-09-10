from __future__ import annotations

import threading
from pathlib import Path

from conftest import one
from runpeek.store import SQLiteStore, open_connection


def _att(kind: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {"attempt_id": "att_1", "operation_id": "op_1", "provider": "openai"}
    base.update(extra)
    return base


def test_terminal_before_start_then_start_fills(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db", "r1", batch_ms=10)
    store.start()
    store.emit("operation", {"operation_id": "op_1", "surface": "x", "attribution_state": "unattributed"})
    store.emit("attempt_end", _att("attempt_end", observed_status="completed", ended_at="2026-01-01T00:00:01",
                                   model_served="m"))
    assert store.flush()
    conn = open_connection(tmp_path / "s.db")
    a = one(conn, "SELECT * FROM attempts")
    assert a["start_missing"] == 1 and a["started_at"] is None and a["observed_status"] == "completed"
    store.emit("attempt_start", _att("attempt_start", started_at="2026-01-01T00:00:00", model_requested="m"))
    assert store.flush()
    a = one(conn, "SELECT * FROM attempts")
    assert a["start_missing"] == 0 and a["started_at"] == "2026-01-01T00:00:00"
    assert a["observed_status"] == "completed"  # start never downgrades a terminal
    store.close()


def test_start_only_stays_in_progress(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db", "r1", batch_ms=10)
    store.start()
    store.emit("attempt_start", _att("attempt_start", started_at="t0"))
    store.close()
    conn = open_connection(tmp_path / "s.db")
    assert one(conn, "SELECT observed_status FROM attempts")["observed_status"] == "in_progress"
    run = one(conn, "SELECT * FROM runs")
    assert run["clean_exit"] == 1 and run["records_written"] == 1 and run["dropped_confirmed"] == 0


def test_queue_overflow_drops_and_counts(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db", "r1", queue_size=5, batch_ms=10)
    store._pause = threading.Event()  # park the writer before its first commit
    store.start()
    accepted = sum(1 for _ in range(25) if store.emit("health", {"kind": "k"}))
    assert accepted <= 5 + 1  # queue capacity (+ one item the writer may have taken in flight)
    assert store.dropped_confirmed >= 19
    store._pause.set()
    counters = store.close(5.0)
    assert counters["dropped_confirmed"] == store.dropped_confirmed
    conn = open_connection(tmp_path / "s.db")
    run = one(conn, "SELECT dropped_confirmed, unflushed_known, clean_exit FROM runs")
    assert run["dropped_confirmed"] >= 19 and run["clean_exit"] == 1


def test_bounded_shutdown_reports_unflushed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db", "r1", queue_size=100, batch_ms=10)
    store._pause = threading.Event()  # writer never commits
    store.start()
    for _ in range(40):
        store.emit("health", {"kind": "k"})
    counters = store.close(deadline_s=0.3)
    assert counters["unflushed_known"] >= 39  # everything still queued or in flight
    conn = open_connection(tmp_path / "s.db")
    run = one(conn, "SELECT unflushed_known, records_written, clean_exit FROM runs")
    assert run["unflushed_known"] == counters["unflushed_known"] and run["records_written"] == 0
    assert run["clean_exit"] == 1  # the run *ended*; the loss is on record
    store._pause.set()


def test_bad_payload_is_a_persist_failure_not_a_crash(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db", "r1", batch_ms=10)
    store.start()
    store.emit("operation", {"operation_id": "op_ok", "surface": "x", "attribution_state": "unattributed"})
    assert store.flush()
    store.emit("nope", {})  # unknown kind → the batch rolls back and is counted
    assert store.flush()
    assert store.persist_failures == 1
    store.close()
    conn = open_connection(tmp_path / "s.db")
    assert one(conn, "SELECT COUNT(*) FROM operations")[0] == 1
    assert one(conn, "SELECT persist_failures FROM runs")["persist_failures"] == 1


def test_emit_after_close_is_dropped_quietly(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db", "r1")
    store.start()
    store.close()
    assert store.emit("health", {"kind": "late"}) is False
