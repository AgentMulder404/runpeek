"""Costing perspectives: a named, persisted set of pricing assumptions.

Every query runs under exactly one perspective and prints it. Perspectives are
compared side by side; they are never summed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .ids import now_iso


@dataclass(frozen=True)
class Perspective:
    perspective_id: str
    rates: str = "list"  # list | contract | reconciled
    resolution: str = "effective_at_execution"
    fallback: str = "nearest_earlier"  # nearest_earlier | nearest | none
    pin: str | None = None


DEFAULT = Perspective("default")


def pinned(rate_card_id: str) -> Perspective:
    """A separate perspective that forces one rate card. The default
    perspective's estimates are untouched by it."""
    return Perspective(perspective_id=f"pinned:{rate_card_id}", pin=rate_card_id)


def ensure(conn: sqlite3.Connection, p: Perspective) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO perspectives (perspective_id, rates, resolution, fallback, pin, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (p.perspective_id, p.rates, p.resolution, p.fallback, p.pin, now_iso()),
    )


def load(conn: sqlite3.Connection, perspective_id: str) -> Perspective | None:
    row = conn.execute(
        "SELECT perspective_id, rates, resolution, fallback, pin FROM perspectives WHERE perspective_id = ?",
        (perspective_id,),
    ).fetchone()
    if row is None:
        return None
    return Perspective(*row)
