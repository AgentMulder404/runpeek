"""Rebuild derived agent data in isolation; retain explicit work and assignments."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .agents import diagnostics
from .agents.events import TranscriptFile
from .agents.ingest import Ingestor
from .store import apply_schema, open_connection


def repair(path: Path) -> Path:
    conn = open_connection(path)
    try:
        sessions = conn.execute("SELECT * FROM agent_sessions").fetchall()
        files = [TranscriptFile(Path(s["transcript_path"]), s["session_id"], s["project_path"],
                                s["parent_session_id"], s["source"]) for s in sessions]
        if any(not f.path.is_file() for f in files):
            raise ValueError("All original session files must be available. Store unchanged.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backup = path.with_name(path.name + ".backup-" + stamp)
        with open_connection(backup) as dest:
            conn.backup(dest)
        # Hold the source write lock throughout rebuilding; another watcher cannot
        # commit between the backup/rebuild and replacement transaction.
        conn.execute("BEGIN IMMEDIATE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rebuilt = open_connection(Path(tmp) / "rebuilt.db")
                apply_schema(rebuilt)
                fingerprint = conn.execute("SELECT value FROM meta WHERE key='fingerprint_key'").fetchone()
                if fingerprint:
                    rebuilt.execute("INSERT INTO meta(key,value) VALUES ('fingerprint_key',?)", (fingerprint[0],))
                ing = Ingestor(rebuilt)
                for tf in files:
                    ing.ingest(tf)
                for tf in files:
                    diagnostics.analyse_session(rebuilt, tf.session_id)
                tables = ["agent_sessions", "agent_turns", "agent_actions", "agent_usage",
                          "agent_usage_duplicates", "agent_findings", "watch_checkpoints"]
                for table in tables:
                    conn.execute(f"DELETE FROM {table}")
                    rows = rebuilt.execute(f"SELECT * FROM {table}")
                    columns = [d[0] for d in rows.description or []]
                    marks = ",".join("?" for _ in columns)
                    conn.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", rows)
                for s in sessions:
                    conn.execute("UPDATE agent_sessions SET customer_id=?, job_name=? WHERE session_id=?",
                                 (s["customer_id"], s["job_name"], s["session_id"]))
                rebuilt.close()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return backup
    finally:
        conn.close()
