"""The foreground watcher: a compact startup block and an append-only feed of
meaningful events. Polling internals stay behind --verbose.

Why polling: the set of files is small (one per session), stat() is cheap,
and polling has no dependency and no missed events across rotation.
"""

from __future__ import annotations

import signal
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import ui
from ..rates import RateCardSet
from ..ui import Term, sanitize
from . import claude_code, diagnostics
from .diagnostics import title_for
from .events import TranscriptFile
from .ingest import Ingestor, IngestStats, history_cutoff

UPDATE_RATE_LIMIT_S = 60.0
WAITING_NOTE_S = 600.0


class Watcher:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        project: str | Path | None,
        all_projects: bool = False,
        history: str = "7d",
        interval_s: float = 2.0,
        rescan_s: float = 10.0,
        cards: RateCardSet | None = None,
        verbose: bool = False,
        term: Term | None = None,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self.conn = conn
        self.project = str(project) if project is not None else None
        self.all_projects = all_projects
        self.history = history
        self.interval_s = interval_s
        self.rescan_s = rescan_s
        self.verbose = verbose
        self.term = term or Term(color=False)
        self.out = out or (lambda s: print(s, flush=True))
        since, none = history_cutoff(history)
        self.since: datetime | None = since
        self.ingestor = Ingestor(conn, cards=cards, history_since=since, history_none=none)
        self.stop = _Stop()
        self._files: dict[str, TranscriptFile] = {}
        self._last_stat: dict[str, tuple[int, int, float]] = {}
        self._failing: set[str] = set()
        self._announced_sessions: set[str] = set()
        self._finding_updates: dict[str, float] = {}
        self._live = False
        self._live_started: float = 0.0
        self._last_event: float = 0.0
        self.totals = IngestStats()
        self.live_totals = IngestStats()
        self.ticks = 0
        self.live_findings = 0

    # ------------------------------------------------------------------ setup

    def discover(self) -> list[TranscriptFile]:
        found = claude_code.discover(self.project, all_projects=self.all_projects)
        new = [tf for tf in found if str(tf.path) not in self._files]
        for tf in found:
            self._files[str(tf.path)] = tf
        return new

    def _review_cmd(self) -> str:
        return "runpeek sessions --all-projects" if self.all_projects else f"runpeek sessions --project {self.project}"

    def _emit(self, s: str = "") -> None:
        self.out(s)

    def banner(self) -> None:
        t = self.term
        scope = "all projects" if self.all_projects else ui.project_name(self.project)
        self._emit(t.bold("RUNPEEK / LIVE WATCH"))
        self._emit("")
        self._emit(f"Watching Claude Code in {scope}")
        self._emit("Use Claude Code normally. This terminal shows activity")
        self._emit("and potential inefficiencies as they appear.")
        self._emit("")
        self._emit(t.green("Local collection · no uploads"))
        self._emit("Prompts and file contents are not stored.")
        self._emit("File paths and usage metadata are stored.")
        if self.verbose:
            self._emit("")
            self._emit(t.dim(f"adapter: claude-code transcripts, built against writer versions "
                             f"{claude_code.INSPECTED_WRITER_VERSIONS} (format undocumented, experimental)"))
            where = (str(claude_code.claude_home() / "projects") if self.all_projects
                     else str(claude_code.project_dir(self.project or "")))
            self._emit(t.dim(f"reading: {sanitize(where)} (read-only)"))
            self._emit(t.dim(f"polling every {self.interval_s:g}s, directory rescan every {self.rescan_s:g}s"))
        self._emit("")

    # ------------------------------------------------------------------- loop

    def _loaded_sessions(self) -> tuple[int, int]:
        """(sessions with activity, empty transcripts) in scope."""
        where = "" if self.all_projects else " WHERE s.project_path = ?"
        params: tuple[Any, ...] = () if self.all_projects else (self.project,)
        rows = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM agent_actions a WHERE a.session_id = s.session_id)"
            "     + (SELECT COUNT(*) FROM agent_usage u WHERE u.session_id = s.session_id) AS n"
            f" FROM agent_sessions s{where}", params,
        ).fetchall()
        active = sum(1 for r in rows if r["n"])
        return active, len(rows) - active

    def catch_up(self) -> None:
        """Historical ingestion. Findings found here are summarised, never replayed as live events."""
        self._emit("Loading recent history…")
        stats = self.tick()
        active, empty = self._loaded_sessions()
        found = len(self._files)
        note = f"Ready · {active} session{'s' if active != 1 else ''} loaded"
        extra = []
        if found != active:
            extra.append(f"{found} transcript file{'s' if found != 1 else ''} found")
        if empty:
            extra.append(f"{empty} empty")
        if stats.skipped_history:
            extra.append(f"{stats.skipped_history} older than {self.history} skipped")
        if extra:
            note += " (" + ", ".join(extra) + ")"
        self._emit(self.term.green(note))
        hist = self.conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT session_id) s FROM agent_findings f WHERE session_id IN"
            " (SELECT session_id FROM agent_sessions" + ("" if self.all_projects else " WHERE project_path = ?") + ")",
            () if self.all_projects else (self.project,),
        ).fetchone()
        if hist["n"]:
            self._emit(f"Historical: {hist['n']} potential inefficiencies in {hist['s']} session"
                       f"{'s' if hist['s'] != 1 else ''} (not replayed here) · runpeek findings"
                       + (" --all-projects" if self.all_projects else f" --project {self.project}"))
        for sid in stats.unknown_version_sessions:
            self._emit(self.term.amber(f"Note: session {sid[:8]} uses a transcript version this adapter was not"
                                       " built for; parsed best-effort."))
        self._emit("")
        self._emit("New activity appears below.")
        self._emit("Ctrl-C stops watching. Your Claude session keeps running.")
        self._emit("")
        self._emit(self.term.dim("Experimental Claude Code integration"))
        self._emit(self.term.dim(f"Review: {self._review_cmd()}"))
        self._emit("")

    def tick(self) -> IngestStats:
        self.ticks += 1
        stats = IngestStats()
        for path, tf in list(self._files.items()):
            try:
                st = tf.path.stat()
            except OSError:
                continue
            sig = (st.st_ino, st.st_size, st.st_mtime)
            if self._last_stat.get(path) == sig:
                continue
            before = (stats.actions, stats.usage, stats.entries)
            try:
                self.ingestor.ingest(tf, stats)
            except Exception as exc:
                if path not in self._failing:
                    self._failing.add(path)
                    self._event(self.term.red("COLLECTION PROBLEM"),
                                [f"Could not read session {tf.session_id[-8:]}: {sanitize(repr(exc))}",
                                 "Collection continues for other sessions; this one is retried on the next poll."])
                continue
            if path in self._failing:
                self._failing.discard(path)
                self._event(self.term.green("COLLECTION RECOVERED"),
                            [f"Session {tf.session_id[-8:]} is readable again."])
            self._last_stat[path] = sig
            added = tuple(a - b for a, b in zip((stats.actions, stats.usage, stats.entries), before, strict=True))
            if added[2]:
                delta = diagnostics.analyse_session(self.conn, tf.session_id)
                self.conn.commit()
                if self._live:
                    self._announce_findings(delta)
                if self.verbose:
                    self._emit(self.term.dim(f"  · {tf.session_id.split('/')[-1][:8]}: {added[2]} transcript lines,"
                                             f" {added[0]} tool calls, {added[1]} model calls"
                                             + (f", {delta.count} finding(s)" if delta.count else "")))
        if stats.reset_files:
            self._event(self.term.amber("TRANSCRIPT REWRITTEN"),
                        [f"{stats.reset_files} file(s) were truncated or rotated and re-read from the start.",
                         "Nothing was duplicated."])
        if stats.oversized_lines:
            self._event(self.term.amber("LINES SKIPPED"),
                        [f"{stats.oversized_lines} transcript line(s) exceeded the size limit and were skipped."])
        _accumulate(self.totals, stats)
        if self._live:
            _accumulate(self.live_totals, stats)
        return stats

    # ------------------------------------------------------------------ events

    def _event(self, title: str, lines: list[str], *, at: str | None = None) -> None:
        stamp = ui.clock(at) if at else datetime.now().strftime("%H:%M")
        self._emit(f"{stamp}  {title}")
        for ln in lines:
            if not ln:
                self._emit("")
                continue
            for w in self.term.wrap(ln, indent=7):
                self._emit(w)
        self._emit("")
        self._last_event = time.monotonic()

    def _announce_session(self, tf: TranscriptFile, at: str | None) -> None:
        if tf.session_id in self._announced_sessions:
            return
        self._announced_sessions.add(tf.session_id)
        what = "NEW SUBAGENT" if tf.is_subagent else "NEW SESSION"
        sid = tf.session_id.split("/")[-1][:8]
        self._event(self.term.green(what),
                    [f"{ui.project_name(tf.project_path)} · started {ui.clock(at)} · session {sid}"], at=at)

    def _on_ingest_event(self, kind: str, tf: TranscriptFile, p: dict[str, Any]) -> None:
        sid = tf.session_id.split("/")[-1][:8]
        if kind == "activity":
            self._announce_session(tf, p.get("at"))
        elif kind == "turn_start":
            self._announce_session(tf, p.get("at"))
            self._event("TURN STARTED", [f"session {sid}"], at=p.get("at"))
        elif kind == "turn_end":
            self._turn_finished(tf, p)

    def _turn_finished(self, tf: TranscriptFile, p: dict[str, Any]) -> None:
        t = self.conn.execute(
            "SELECT t.tool_calls, t.assistant_messages,"
            " (SELECT COUNT(*) FROM agent_actions a WHERE a.turn_id = t.turn_id AND a.is_error = 1) errs,"
            " (SELECT COALESCE(SUM(api_equiv_nanos),0) FROM agent_usage u WHERE u.turn_id = t.turn_id"
            "    AND api_equiv_status = 'priced') equiv,"
            " (SELECT COUNT(*) FROM agent_usage u WHERE u.turn_id = t.turn_id"
            "    AND api_equiv_status != 'priced') unpriced,"
            " (SELECT COUNT(*) FROM agent_findings f WHERE f.turn_id = t.turn_id) items"
            " FROM agent_turns t WHERE t.turn_id = ?", (p["turn_id"],),
        ).fetchone()
        if t is None:
            return
        dur = ui.duration(p["duration_ms"] / 1000) if p.get("duration_ms") else "duration not reported"
        lines = [f"{dur} · {t['tool_calls']} tool calls" + (f" ({t['errs']} failed)" if t["errs"] else "")
                 + f" · {t['assistant_messages']} model calls"]
        if t["assistant_messages"]:
            cost = ui.usd_exact(t["equiv"]) + (f" (+{t['unpriced']} calls not priced)" if t["unpriced"] else "")
            lines.append(f"API-equivalent estimate: {cost}")
        if t["items"]:
            lines.append(f"{t['items']} potential inefficienc{'ies' if t['items'] != 1 else 'y'} to review"
                         f" · runpeek session {tf.session_id.split('/')[-1][:8]}")
        self._event("TURN FINISHED", lines, at=p.get("at"))

    def _announce_findings(self, delta: diagnostics.Delta) -> None:
        now = time.monotonic()
        for fid in delta.new + delta.updated:
            f = self.conn.execute("SELECT * FROM agent_findings WHERE finding_id = ?", (fid,)).fetchone()
            if f is None:
                continue
            updated = fid in delta.updated
            if updated:
                last = self._finding_updates.get(fid, 0.0)
                if now - last < UPDATE_RATE_LIMIT_S:
                    continue
            self._finding_updates[fid] = now
            counts = __import__("json").loads(f["counts"])
            title = title_for(f["kind"], counts) + (" (updated)" if updated else "")
            sid = str(f["session_id"]).split("/")[-1][:8]
            lines = [sanitize(f["summary"])]
            if f["kind"] == "repeated_read":
                lines.append("No edit to that file was observed between reads.")
                lines.append("Repeated billing cannot be determined.")
            elif f["kind"] == "repeated_failing_action":
                lines.append("No edit or write was observed between the failures.")
                lines.append(sanitize(f["suggestion"]))
            else:
                lines.append(sanitize(f["suggestion"]))
            lines.append("")
            lines.append(f"Evidence: runpeek session {sid}")
            self._event(self.term.amber(title), lines, at=f["last_at"])
            self.live_findings += 1

    # -------------------------------------------------------------------- run

    def run(self, *, once: bool = False, max_ticks: int | None = None) -> IngestStats:
        self.discover()
        self.banner()
        self.catch_up()
        if once:
            self._emit(self._stopped_line())
            return self.totals
        self._live = True
        self._live_started = time.monotonic()
        self._last_event = time.monotonic()
        self.ingestor.on_event = self._on_ingest_event
        last_scan = time.monotonic()
        while not self.stop.is_set():
            if max_ticks is not None and self.ticks >= max_ticks:
                break
            self.stop.wait(self.interval_s)
            if self.stop.is_set():
                break
            self.tick()
            if time.monotonic() - last_scan >= self.rescan_s:
                self.discover()  # new files are announced on their first activity
                last_scan = time.monotonic()
            if time.monotonic() - self._last_event >= WAITING_NOTE_S:
                lt = self.live_totals
                self._event(self.term.dim("Waiting for new activity"),
                            [f"since watching started: {lt.actions} tool calls · {lt.usage} model calls"
                             f" · {self.live_findings} items to review"])
        self._emit(self._stopped_line())
        return self.totals

    def _stopped_line(self) -> str:
        if not self._live:
            return (f"Done. Loaded {self.totals.actions} tool calls · {self.totals.usage} model calls."
                    f"\nReview: {self._review_cmd()}")
        lt = self.live_totals
        return (f"Stopped watching. Since watching started: {lt.actions} tool calls · {lt.usage} model calls"
                f" · {self.live_findings} new items to review\nReview: {self._review_cmd()}")

    def install_signal_handlers(self) -> None:
        def _stop(signum: int, _frame: object) -> None:
            self.stop.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, _stop)
            except ValueError:  # not the main thread
                pass


class _Stop:
    """A tiny Event that also works when the watcher runs on a non-main thread."""

    def __init__(self) -> None:
        import threading

        self._e = threading.Event()

    def set(self) -> None:
        self._e.set()

    def is_set(self) -> bool:
        return self._e.is_set()

    def wait(self, timeout: float) -> bool:
        return self._e.wait(timeout)


def _accumulate(total: IngestStats, s: IngestStats) -> None:
    for k in ("files_seen", "files_changed", "entries", "unparseable", "actions", "usage", "turns", "reset_files",
              "skipped_history", "oversized_lines"):
        setattr(total, k, getattr(total, k) + getattr(s, k))
    for sid in s.unknown_version_sessions:
        if sid not in total.unknown_version_sessions:
            total.unknown_version_sessions.append(sid)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
