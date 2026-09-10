"""Checkpointed, idempotent ingestion of transcript files into the store.

Every persisted row is keyed by an identifier the source already assigned
(session id, prompt id, tool_use id, message id), so re-reading a file — after
a restart, a truncation, or a rotation — never duplicates anything.

Partial writes: only lines terminated by a newline are consumed; the
checkpoint offset always points at the start of the first unconsumed line.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..ids import new_id, now_iso
from ..money import tokens_cost_nanos
from ..rates import RateCardSet
from . import claude_code
from .events import (
    Event,
    SessionInfo,
    ToolResultEvent,
    ToolUseEvent,
    TranscriptFile,
    TurnDurationEvent,
    TurnStartEvent,
    Unparseable,
    UsageEvent,
)

CALC_VERSION = 1
MAX_LINE_BYTES = 16 * 1024 * 1024  # a single line longer than this is skipped, visibly
HEAD_BYTES = 4096  # hashed at checkpoint time; a changed head means the file was rewritten, not appended


def _head_sha(path: Path, limit: int) -> str | None:
    """sha256 of the first min(limit, HEAD_BYTES) bytes, or None if unreadable."""
    n = min(limit, HEAD_BYTES)
    if n <= 0:
        return None
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read(n)).hexdigest()
    except OSError:
        return None


@dataclass
class IngestStats:
    files_seen: int = 0
    files_changed: int = 0
    entries: int = 0
    unparseable: int = 0
    actions: int = 0
    usage: int = 0
    turns: int = 0
    reset_files: int = 0  # truncated or rotated
    skipped_history: int = 0
    unknown_version_sessions: list[str] = field(default_factory=list)
    oversized_lines: int = 0


def fingerprint_key(conn: sqlite3.Connection) -> bytes:
    """Per-store random key for action fingerprints. Lives in `meta`; exports
    never include `meta`, so fingerprints cannot be recomputed elsewhere."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'fingerprint_key'").fetchone()
    if row:
        return bytes.fromhex(row[0])
    key = secrets.token_bytes(32)
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('fingerprint_key', ?)", (key.hex(),))
    row = conn.execute("SELECT value FROM meta WHERE key = 'fingerprint_key'").fetchone()
    return bytes.fromhex(row[0])


def fingerprint(key: bytes, payload: dict[str, Any]) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hmac.new(key, canon, hashlib.sha256).hexdigest()[:24]


class Ingestor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        cards: RateCardSet | None = None,
        history_since: datetime | None = None,
        history_none: bool = False,
        on_event: Callable[[str, TranscriptFile, dict[str, Any]], None] | None = None,
    ) -> None:
        self.conn = conn
        # Optional live sink: ("turn_start" | "turn_end" | "activity", file, payload).
        # The watcher attaches it only after historical catch-up, so history is
        # never replayed as live events.
        self.on_event = on_event
        self._active: set[str] = set()
        self.cards = cards or RateCardSet.builtin()
        self.key = fingerprint_key(conn)
        self.history_since = history_since
        self.history_none = history_none
        self._parsers: dict[str, claude_code.Parser] = {}
        self._seq: dict[str, int] = {}

    # ------------------------------------------------------------------ files

    def ingest(self, tf: TranscriptFile, stats: IngestStats | None = None) -> IngestStats:
        stats = stats or IngestStats()
        stats.files_seen += 1
        try:
            st = os.stat(tf.path)
        except OSError:
            return stats
        cp = self.conn.execute(
            "SELECT inode, size, offset, line_no, head_sha FROM watch_checkpoints WHERE transcript_path = ?",
            (str(tf.path),),
        ).fetchone()
        offset, line_no = 0, 0
        if cp is None:
            # First sight: history policy decides where to start.
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            if self.history_none or (self.history_since is not None and mtime < self.history_since):
                # Skipped by the history policy: checkpoint at the end so only new lines are read
                # later, but create no session row — an unread transcript is not an "empty" session.
                stats.skipped_history += 1
                self._checkpoint(tf, st, st.st_size, -1)  # -1: line numbers unknown from here on
                return stats
        else:
            offset, line_no = int(cp["offset"]), int(cp["line_no"])
            rewritten = (
                cp["inode"] not in (None, st.st_ino)  # rotation with a new inode
                or st.st_size < offset  # truncation
                # rewrite that reused the inode (common on Linux): the head bytes no longer match
                or (cp["head_sha"] is not None and offset > 0 and _head_sha(tf.path, offset) != cp["head_sha"])
            )
            if rewritten:
                offset, line_no = 0, 0
                stats.reset_files += 1
                self._parsers.pop(tf.session_id, None)
        if st.st_size == offset and cp is not None:
            self._checkpoint(tf, st, offset, line_no)
            return stats
        stats.files_changed += 1
        parser = self._parsers.get(tf.session_id)
        if parser is None:
            parser = claude_code.Parser(tf.session_id)
            self._parsers[tf.session_id] = parser
            if offset > 0:
                # resuming mid-file: recover cwd/version cheaply from the first line
                self._prime(parser, tf.path)
        self._ensure_session(tf)
        self.conn.execute("BEGIN")
        try:
            with open(tf.path, "rb") as fh:
                fh.seek(offset)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break  # partial write; wait for the newline
                    if len(line) > MAX_LINE_BYTES:
                        stats.oversized_lines += 1
                        offset += len(line)
                        line_no += 1
                        continue
                    text = line.decode("utf-8", errors="replace")
                    if text.strip():
                        stats.entries += 1
                        for ev in parser.parse_line(text, line_no):
                            self._apply(tf, parser, ev, stats)
                    offset += len(line)
                    line_no += 1
            self._finish_session(tf, parser, stats)
            self._checkpoint(tf, st, offset, line_no)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return stats

    def _prime(self, parser: claude_code.Parser, path: Path) -> None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    for _ in parser.parse_line(line, i):
                        pass
                    if parser.cwd and parser.version:
                        break
                    if i > 50:
                        break
        except OSError:
            pass

    def _checkpoint(self, tf: TranscriptFile, st: os.stat_result, offset: int, line_no: int) -> None:
        self.conn.execute(
            "INSERT INTO watch_checkpoints (transcript_path, session_id, inode, size, offset, line_no, head_sha,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(transcript_path) DO UPDATE SET inode = excluded.inode,"
            " size = excluded.size, offset = excluded.offset, line_no = excluded.line_no,"
            " head_sha = excluded.head_sha, updated_at = excluded.updated_at",
            (str(tf.path), tf.session_id, st.st_ino, st.st_size, offset, line_no, _head_sha(tf.path, offset),
             now_iso()),
        )

    # --------------------------------------------------------------- sessions

    def _ensure_session(self, tf: TranscriptFile) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO agent_sessions (session_id, source, project_path, transcript_path,"
            " parent_session_id, is_subagent, first_seen_at) VALUES (?,?,?,?,?,?,?)",
            (tf.session_id, claude_code.SOURCE, tf.project_path, str(tf.path), tf.parent_session_id,
             1 if tf.is_subagent else 0, now_iso()),
        )
        if tf.session_id not in self._seq:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM agent_actions WHERE session_id = ?", (tf.session_id,)
            ).fetchone()
            self._seq[tf.session_id] = int(row[0])

    def _finish_session(self, tf: TranscriptFile, parser: claude_code.Parser, stats: IngestStats) -> None:
        status = "supported" if claude_code.version_supported(parser.version) else "unknown_version"
        if status == "unknown_version" and tf.session_id not in stats.unknown_version_sessions:
            stats.unknown_version_sessions.append(tf.session_id)
        self.conn.execute(
            "UPDATE agent_sessions SET source_version = COALESCE(?, source_version), format_status = ?,"
            " project_path = COALESCE(project_path, ?), last_ingested_at = ?,"
            " entries_ingested = entries_ingested + ?, entries_unparseable = entries_unparseable + ?"
            " WHERE session_id = ?",
            (parser.version, status, parser.cwd, now_iso(), stats.entries, stats.unparseable, tf.session_id),
        )
        # derived bounds from what is stored
        self.conn.execute(
            "UPDATE agent_sessions SET"
            " first_event_at = (SELECT MIN(requested_at) FROM agent_actions WHERE session_id = ?),"
            " last_event_at = (SELECT MAX(COALESCE(completed_at, requested_at)) FROM agent_actions"
            "                  WHERE session_id = ?)"
            " WHERE session_id = ?",
            (tf.session_id, tf.session_id, tf.session_id),
        )
        self.conn.execute(
            "UPDATE agent_sessions SET"
            " first_event_at = COALESCE(first_event_at, (SELECT MIN(at) FROM agent_usage WHERE session_id = ?)),"
            " last_event_at = COALESCE((SELECT MAX(at) FROM agent_usage WHERE session_id = ?), last_event_at)"
            " WHERE session_id = ?",
            (tf.session_id, tf.session_id, tf.session_id),
        )

    # ----------------------------------------------------------------- events

    def _apply(self, tf: TranscriptFile, parser: claude_code.Parser, ev: Event, stats: IngestStats) -> None:
        sid = tf.session_id
        if isinstance(ev, Unparseable):
            stats.unparseable += 1
        elif isinstance(ev, SessionInfo):
            pass  # applied in _finish_session from parser state
        elif isinstance(ev, TurnStartEvent):
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO agent_turns (turn_id, session_id, started_at) VALUES (?,?,?)",
                (ev.turn_id, sid, ev.at),
            )
            stats.turns += cur.rowcount if cur.rowcount > 0 else 0
            if cur.rowcount > 0:
                self._emit("turn_start", tf, {"turn_id": ev.turn_id, "at": ev.at})
        elif isinstance(ev, TurnDurationEvent):
            if parser.current_turn:
                self.conn.execute(
                    "UPDATE agent_turns SET duration_ms = COALESCE(?, duration_ms), ended_at = COALESCE(?, ended_at)"
                    " WHERE turn_id = ?",
                    (ev.duration_ms, ev.at, parser.current_turn),
                )
                self._emit("turn_end", tf, {"turn_id": parser.current_turn, "at": ev.at,
                                            "duration_ms": ev.duration_ms})
        elif isinstance(ev, ToolUseEvent):
            fp = fingerprint(self.key, ev.fingerprint_input)
            self._seq[sid] = self._seq.get(sid, 0) + 1
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO agent_actions (action_id, session_id, turn_id, sequence, tool_name, action_kind,"
                " target, fingerprint, requested_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ev.tool_use_id, sid, parser.current_turn, self._seq[sid], ev.tool_name, ev.action_kind, ev.target,
                 fp, ev.at),
            )
            if cur.rowcount > 0:
                stats.actions += 1
                self._activity(tf, ev.at)
                if parser.current_turn:
                    self.conn.execute("UPDATE agent_turns SET tool_calls = tool_calls + 1 WHERE turn_id = ?",
                                      (parser.current_turn,))
            else:
                self._seq[sid] -= 1
        elif isinstance(ev, ToolResultEvent):
            self.conn.execute(
                "UPDATE agent_actions SET completed_at = COALESCE(completed_at, ?), is_error = ?,"
                " duration_ms = CASE WHEN requested_at IS NOT NULL AND ? IS NOT NULL THEN"
                "   (julianday(?) - julianday(requested_at)) * 86400000.0 ELSE duration_ms END"
                " WHERE action_id = ?",
                (ev.at, 1 if ev.is_error else 0, ev.at, ev.at, ev.tool_use_id),
            )
        elif isinstance(ev, UsageEvent):
            if self._usage(sid, parser, ev, stats):
                self._activity(tf, ev.at)

    def _emit(self, kind: str, tf: TranscriptFile, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, tf, payload)
        except Exception:
            pass

    def _activity(self, tf: TranscriptFile, at: str | None) -> None:
        """First observed activity for a session in this ingestor's lifetime."""
        if tf.session_id in self._active:
            return
        self._active.add(tf.session_id)
        self._emit("activity", tf, {"at": at})

    def _usage(self, sid: str, parser: claude_code.Parser, ev: UsageEvent, stats: IngestStats) -> bool:
        amount: int | None = None
        status = "no_usage" if not ev.has_usage else "unpriced"
        card_id: str | None = None
        resolution: str | None = None
        if ev.has_usage:
            at = _parse_ts(ev.at)
            card, resolution = self.cards.resolve(provider=claude_code.PROVIDER, source="list", at=at,
                                                  fallback="nearest_earlier", pin=None)
            if card is not None:
                card_id = card.rate_card_id
                _, prices = card.prices_for(ev.model)
                if prices is not None:
                    amount = (
                        tokens_cost_nanos(ev.input_tokens or 0, prices.input)
                        + tokens_cost_nanos(ev.cache_write_5m_tokens or 0, prices.cache_write_5m or prices.input)
                        + tokens_cost_nanos(ev.cache_write_1h_tokens or 0, prices.cache_write_1h or prices.input)
                        + tokens_cost_nanos(ev.cache_read_tokens or 0, prices.cached_input)
                        + tokens_cost_nanos(ev.output_tokens or 0, prices.output)
                    )
                    status = "priced"
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO agent_usage (usage_id, session_id, turn_id, request_id, model, at, input_tokens,"
            " cache_write_5m_tokens, cache_write_1h_tokens, cache_read_tokens, output_tokens, web_search_requests,"
            " web_fetch_requests, usage_kind, provenance, source_cost_nanos, api_equiv_nanos, api_equiv_status,"
            " rate_card_id, rate_resolution, calc_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.usage_id, sid, parser.current_turn, ev.request_id, ev.model, ev.at, ev.input_tokens,
             ev.cache_write_5m_tokens, ev.cache_write_1h_tokens, ev.cache_read_tokens, ev.output_tokens,
             ev.web_search_requests, ev.web_fetch_requests, ev.usage_kind, ev.provenance, ev.source_cost_nanos,
             amount, status, card_id, resolution, CALC_VERSION),
        )
        if cur.rowcount > 0:
            stats.usage += 1
            if parser.current_turn:
                self.conn.execute(
                    "UPDATE agent_turns SET assistant_messages = assistant_messages + 1 WHERE turn_id = ?",
                    (parser.current_turn,),
                )
            return True
        return False


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def history_cutoff(spec: str) -> tuple[datetime | None, bool]:
    """'none' → skip existing content; 'all' → everything; '7d'/'24h' → mtime cutoff."""
    if spec == "none":
        return None, True
    if spec == "all":
        return None, False
    unit = spec[-1]
    n = int(spec[:-1])
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
    return datetime.now(timezone.utc) - delta, False


def new_finding_id() -> str:
    return new_id("fnd")
