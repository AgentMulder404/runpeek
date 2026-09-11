"""Bounded, non-blocking accounting SDK for custom orchestrators.

Propagate Tracker.context() explicitly across process/network boundaries. Async
Python tasks inherit contextvars; threads should receive context explicitly.
"""
from __future__ import annotations

import contextlib
import contextvars
import queue
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from . import ledger
from .money import tokens_cost_nanos
from .rates import RateCardSet
from .store import open_connection

_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("runpeek_tracking", default=None)


@contextlib.contextmanager
def task(work_item_id: str, *, session_id: str | None = None,
         parent_session_id: str | None = None) -> Iterator[dict[str, str]]:
    context = {"work_item_id": work_item_id, "session_id": session_id or str(uuid.uuid4())}
    if parent_session_id:
        context["parent_session_id"] = parent_session_id
    token = _context.set(context)
    try:
        yield context.copy()
    finally:
        _context.reset(token)


class Tracker:
    def __init__(self, db: str | Path, *, agent: str = "custom", source: str = "sdk",
                 capacity: int = 1024, workspace: str = "local") -> None:
        if capacity < 1:
            raise ValueError("Queue capacity must be positive")
        self.db, self.agent, self.source, self.workspace = Path(db), agent, source, workspace
        self.dropped = self.persist_failures = self.written = self.invalid = 0
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="runpeek-accounting", daemon=True)
        self._worker.start()
        self._cards = RateCardSet.builtin()

    @staticmethod
    def context() -> dict[str, str]:
        return (_context.get() or {}).copy()

    def record(self, *, provider: str, model: str, request_id: str | None = None,
               input_tokens: int | None = None, cache_read_tokens: int | None = None,
               cache_write_tokens: int | None = None, output_tokens: int | None = None,
               reasoning_tokens: int | None = None, amount_nanos: int | None = None,
               basis: str = "estimated", account_scope: str = "default",
               context: dict[str, str] | None = None, event_id: str | None = None) -> bool:
        """Input excludes cached tokens; output includes reasoning. Never pass content.

        Invalid telemetry is counted and discarded rather than interrupting work.
        Supply actual/allocated amounts explicitly; only estimates are calculated.
        """
        try:
            ctx = self.context() if context is None else context.copy()
            at = ledger.now()
            rate_card = None
            if basis == "estimated" and amount_nanos is None and any(
                v is not None for v in (input_tokens, cache_read_tokens, output_tokens)
            ):
                card, _ = self._cards.resolve(provider=provider, source="list", at=datetime.fromisoformat(at),
                                               fallback="nearest_earlier", pin=None)
                if card:
                    _, prices = card.prices_for(model)
                    if prices and not cache_write_tokens:  # TTL-specific write pricing needs explicit amount.
                        rate_card = card.rate_card_id
                        amount_nanos = (tokens_cost_nanos(input_tokens or 0, prices.input)
                                        + tokens_cost_nanos(cache_read_tokens or 0, prices.cached_input)
                                        + tokens_cost_nanos(output_tokens or 0, prices.output))
            event = ledger.validate(dict(ctx, schema_version=1, source=self.source, agent=self.agent,
                event_id=event_id or str(uuid.uuid4()), provider=provider, model=model, at=at,
                account_scope=account_scope, request_id=request_id, basis=basis, amount_nanos=amount_nanos,
                input_tokens=input_tokens, cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens, output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens, rate_card=rate_card))
        except (TypeError, ValueError):
            with self._lock:
                self.invalid += 1
            return False
        with self._lock:
            if self._closed:
                self.dropped += 1
                return False
            try:
                self._queue.put_nowait(event)
                return True
            except queue.Full:
                self.dropped += 1
                return False

    def _run(self) -> None:
        conn = None
        try:
            conn = open_connection(self.db)
            ledger.setup(conn)
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    paused = conn.execute(
                        "SELECT value FROM ledger_settings WHERE key='collection_paused'"
                    ).fetchone()
                    if paused and paused[0] == "true":
                        self.dropped += 1
                        continue
                    ledger.ingest(conn, self.workspace, [event])
                    self.written += 1
                except Exception:
                    self.persist_failures += 1
                finally:
                    self._queue.task_done()
        except Exception:
            # Fail open: drain until close so a failed storage setup cannot make
            # instrumentation block the application or retain unbounded events.
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    self._queue.get(timeout=0.1)
                    self.persist_failures += 1
                    self._queue.task_done()
                except queue.Empty:
                    pass
        finally:
            if conn:
                conn.close()

    def close(self, timeout: float = 5.0) -> dict[str, int]:
        with self._lock:
            self._closed = True
        self._stop.set()
        self._worker.join(timeout=max(timeout, 0))
        return {"written": self.written, "dropped": self.dropped, "invalid": self.invalid,
                "persist_failures": self.persist_failures, "unflushed": self._queue.unfinished_tasks}

    def __enter__(self) -> Tracker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
