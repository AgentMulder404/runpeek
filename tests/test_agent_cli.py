from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_fixtures import SENTINELS, Transcript

PROJECT = "/work/cli-project"


def _run(args: list[str], *, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["RUNPEEK_CLAUDE_HOME"] = str(home)
    return subprocess.run([sys.executable, "-m", "runpeek.cli", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=120)


def test_watch_once_sessions_session_findings_export(tmp_path: Path) -> None:
    home = tmp_path / "home"
    t = Transcript(home, PROJECT)
    t.user_prompt()
    for _ in range(3):
        t.bash("npm test", is_error=True, seconds=20)
    t.turn_duration(65_000)
    db = tmp_path / "n.db"

    r = _run(["watch", "--db", str(db), "--project", PROJECT, "--history", "all", "--once"], cwd=tmp_path, home=home)
    assert r.returncode == 0, r.stderr
    assert "RUNPEEK / LIVE WATCH" in r.stdout and "Watching Claude Code and Codex in cli-project" in r.stdout
    assert "Local collection · no uploads" in r.stdout and "Ready · 1 session loaded" in r.stdout
    assert "Historical: 1 potential inefficiencies in 1 session" in r.stdout
    assert "Done. Loaded 3 tool calls" in r.stdout and f"Review: runpeek sessions --project {PROJECT}" in r.stdout
    assert "entries" not in r.stdout  # parser vocabulary never reaches the default screen

    s = _run(["sessions", "--db", str(db), "--project", PROJECT], cwd=tmp_path, home=home)
    assert s.returncode == 0 and "RECENT CODING-AGENT SESSIONS" in s.stdout and t.session_id[:8] in s.stdout
    assert f"runpeek session {t.session_id[:8]}" in s.stdout and "Tool calls" in s.stdout
    sd = _run(["sessions", "--db", str(db), "--project", PROJECT, "--detailed"], cwd=tmp_path, home=home)
    assert "API-equivalent estimate" in sd.stdout and "Est. cost" in sd.stdout

    d = _run(["session", "--db", str(db), t.session_id[:8]], cwd=tmp_path, home=home)
    assert d.returncode == 0
    assert f"SESSION {t.session_id[:8]}" in d.stdout and "REPEATED FAILURE" in d.stdout and "npm" in d.stdout
    assert "Check the error before repeating the command." in d.stdout
    assert "Not your subscription charge" in d.stdout and "Source-reported cost: not available" in d.stdout
    assert "1 turns" in d.stdout and "1 min 05 s" in d.stdout
    assert "npm test" not in d.stdout  # commands never shown
    for sentinel in SENTINELS:
        assert sentinel not in d.stdout

    m = _run(["session", "--db", str(db), t.session_id[:8], "--set-customer", "acme", "--set-job", "refactor",
              "--findings-only"], cwd=tmp_path, home=home)
    assert "mapped explicitly" in m.stdout and "Label: customer acme · job refactor (set by you)" in m.stdout

    f = _run(["findings", "--db", str(db), "--project", PROJECT], cwd=tmp_path, home=home)
    assert f.returncode == 0 and "POTENTIAL INEFFICIENCIES TO REVIEW" in f.stdout and "1 shown" in f.stdout
    assert "potential inefficiency, not proven waste" in f.stdout

    out = tmp_path / "x.jsonl"
    x = _run(["export", "--db", str(db), "--out", str(out)], cwd=tmp_path, home=home)
    assert x.returncode == 0
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    kinds = {rec["record"] for rec in recs}
    assert {"agent_sessions", "agent_actions", "agent_usage", "agent_findings", "watch_checkpoints"} <= kinds
    assert "meta" not in kinds  # the fingerprint key never leaves the store
    text = out.read_text()
    for sentinel in SENTINELS:
        assert sentinel not in text
    assert "npm test" not in text  # commands are reduced to a program name


def test_watch_reports_missing_project_dir_and_unsupported_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    r = _run(["watch", "--db", str(tmp_path / "n.db"), "--project", "/nowhere", "--once"], cwd=tmp_path, home=home)
    assert r.returncode == 0 and "Ready · 0 sessions loaded" in r.stdout
    r2 = _run(["watch", "--db", str(tmp_path / "n.db"), "--source", "cursor", "--once"], cwd=tmp_path, home=home)
    assert r2.returncode != 0 and "invalid choice" in r2.stderr
    # codex is a valid source; with no ~/.codex it simply finds nothing
    r3 = _run(["watch", "--db", str(tmp_path / "n.db"), "--source", "codex", "--project", "/nowhere", "--once"],
              cwd=tmp_path, home=home)
    assert r3.returncode == 0 and "Watching Codex in nowhere" in r3.stdout
