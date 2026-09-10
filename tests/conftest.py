from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import OpenAI

from runpeek import accounting, context
from runpeek.instrumentation import openai_chat
from runpeek.perspective import DEFAULT, Perspective
from runpeek.rates import RateCardSet
from runpeek.store import SQLiteStore, open_connection

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from _mock_openai import chat_json, make_client  # noqa: E402

__all__ = ["chat_json", "make_client"]


@dataclass
class Harness:
    store: SQLiteStore
    db: Path

    @property
    def run_id(self) -> str:
        return self.store.run_id

    def conn(self) -> sqlite3.Connection:
        assert self.store.flush(5.0), "store did not flush"
        return open_connection(self.db)

    def account(self, perspective: Perspective = DEFAULT, cards: RateCardSet | None = None) -> sqlite3.Connection:
        conn = self.conn()
        accounting.run(conn, perspective, cards or RateCardSet.builtin())
        return conn


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    db = tmp_path / "runpeek.db"
    store = SQLiteStore(db, "run_test", command="pytest", queue_size=10_000, batch_ms=20)
    store.start()
    context.set_emitter(store.emit)
    openai_chat.install(store.emit)
    try:
        yield Harness(store=store, db=db)
    finally:
        openai_chat._FAULT = None
        openai_chat.uninstall()
        context.set_emitter(None)
        store.close(5.0)


def respond(
    body: dict[str, Any], *, status: int = 200, request_id: str | None = "req-test"
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "application/json"}
        if request_id:
            headers["x-request-id"] = request_id
        return httpx.Response(status, headers=headers, content=json.dumps(body).encode())

    return handler


def client_for(body: dict[str, Any], **kw: Any) -> OpenAI:
    return make_client(respond(body, **kw))


def one(conn: sqlite3.Connection, sql: str, *params: Any) -> Any:
    return conn.execute(sql, params).fetchone()


def rows(conn: sqlite3.Connection, sql: str, *params: Any) -> list[Any]:
    return conn.execute(sql, params).fetchall()
