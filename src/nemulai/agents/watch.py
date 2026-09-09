"""The foreground watcher: bounded polling over discovered transcripts.

Why polling: the set of files is small (one per session), stat() is cheap,
and polling has no dependency, no kernel-event edge cases and no missed
events across rotation. A filesystem watcher is not justified at this scale.
"""

from __future__ import annotations

import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..rates import RateCardSet
from . import claude_code, diagnostics
from .events import TranscriptFile
from .ingest import Ingestor, IngestStats, history_cutoff


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
        out: Callable[[str], None] = lambda s: print(s, flush=True),
    ) -> None:
        self.conn = conn
        self.project = str(project) if project is not None else None
        self.all_projects = all_projects
        self.history = history
        self.interval_s = interval_s
        self.rescan_s = rescan_s
        self.out = out
        since, none = history_cutoff(history)
        self.since: datetime | None = since
        self.ingestor = Ingestor(conn, cards=cards, history_since=since, history_none=none)
        self.stop = threading.Event()
        self._files: dict[str, TranscriptFile] = {}
        self._last_stat: dict[str, tuple[int, int, float]] = {}
        self.totals = IngestStats()
        self.ticks = 0

    # ------------------------------------------------------------------ setup

    def discover(self) -> list[TranscriptFile]:
        found = claude_code.discover(self.project, all_projects=self.all_projects)
        new = [tf for tf in found if str(tf.path) not in self._files]
        for tf in found:
            self._files[str(tf.path)] = tf
        return new

    def banner(self) -> None:
        pdir = "all projects under " + str(claude_code.claude_home() / "projects") if self.all_projects else (
            f"{self.project}  →  {claude_code.project_dir(self.project or '')}")
        self.out(f"nemulai watch · source claude-code (adapter built against transcript writer versions "
                 f"{claude_code.INSPECTED_WRITER_VERSIONS}; format undocumented, adapter experimental)")
        self.out(f"  project   {pdir}")
        if self.since:
            hist_note = f" (files modified before {self.since:%Y-%m-%d %H:%M} UTC are skipped)"
        elif self.history == "none":
            hist_note = " (existing content is skipped)"
        else:
            hist_note = ""
        self.out(f"  history   {self.history}{hist_note}")
        self.out(f"  polling   every {self.interval_s:g}s; directory rescan every {self.rescan_s:g}s; read-only")
        self.out(f"  sessions  {len(self._files)} transcript file(s) found"
                 + (f": {', '.join(tf.session_id.split('/')[-1][:8] for tf in list(self._files.values())[:8])}"
                    + (" …" if len(self._files) > 8 else "") if self._files else ""))
        self.out("  stored    allowlisted metadata only — no prompts, tool payloads or file contents")
        self.out("  stop      Ctrl-C")

    # ------------------------------------------------------------------- loop

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
            except Exception as exc:  # never die on one bad file
                self.out(f"  ! {tf.session_id[:8]}: ingest failed: {exc!r}")
                continue
            self._last_stat[path] = sig
            added = tuple(a - b for a, b in zip((stats.actions, stats.usage, stats.entries), before, strict=True))
            if added[2]:
                new_findings = diagnostics.analyse_session(self.conn, tf.session_id)
                self.conn.commit()
                self.out(f"  + {tf.session_id.split('/')[-1][:8]}: {added[2]} entries, {added[0]} actions,"
                         f" {added[1]} requests" + (f", {new_findings} new finding(s)" if new_findings else ""))
        for sid in stats.unknown_version_sessions:
            self.out(f"  ! {sid[:8]}: transcript writer version not in {claude_code.SUPPORTED_MAJOR_MINOR};"
                     " parsed best-effort, marked unknown_version")
        if stats.reset_files:
            self.out(f"  ! {stats.reset_files} file(s) truncated or rotated; re-read from the start (no duplicates)")
        if stats.oversized_lines:
            self.out(f"  ! {stats.oversized_lines} line(s) over the size limit were skipped")
        _accumulate(self.totals, stats)
        return stats

    def run(self, *, once: bool = False, max_ticks: int | None = None) -> IngestStats:
        self.discover()
        self.banner()
        last_scan = time.monotonic()
        while not self.stop.is_set():
            self.tick()
            if once or (max_ticks is not None and self.ticks >= max_ticks):
                break
            if time.monotonic() - last_scan >= self.rescan_s:
                for tf in self.discover():
                    self.out(f"  new session {tf.session_id.split('/')[-1][:8]}")
                last_scan = time.monotonic()
            self.stop.wait(self.interval_s)
        t = self.totals
        self.out(f"watch stopped after {self.ticks} tick(s): {t.entries} entries, {t.actions} actions,"
                 f" {t.usage} requests ingested; {t.skipped_history} file(s) skipped by history policy")
        return self.totals

    def install_signal_handlers(self) -> None:
        def _stop(signum: int, _frame: object) -> None:
            self.stop.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, _stop)
            except ValueError:  # not the main thread
                pass


def _accumulate(total: IngestStats, s: IngestStats) -> None:
    for k in ("files_seen", "files_changed", "entries", "unparseable", "actions", "usage", "turns", "reset_files",
              "skipped_history", "oversized_lines"):
        setattr(total, k, getattr(total, k) + getattr(s, k))
    for sid in s.unknown_version_sessions:
        if sid not in total.unknown_version_sessions:
            total.unknown_version_sessions.append(sid)


def stderr(msg: str) -> None:
    sys.stderr.write(msg + "\n")
