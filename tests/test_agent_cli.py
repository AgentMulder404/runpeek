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
    env["NEMULAI_CLAUDE_HOME"] = str(home)
    return subprocess.run([sys.executable, "-m", "nemulai.cli", *args], cwd=cwd, env=env, capture_output=True,
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
    assert "source claude-code" in r.stdout and "1 transcript file(s) found" in r.stdout
    assert "1 new finding(s)" in r.stdout and "watch stopped after 1 tick" in r.stdout

    s = _run(["sessions", "--db", str(db), "--project", PROJECT], cwd=tmp_path, home=home)
    assert s.returncode == 0 and t.session_id[:8] in s.stdout and "API-equivalent" in s.stdout

    d = _run(["session", "--db", str(db), t.session_id[:8]], cwd=tmp_path, home=home)
    assert d.returncode == 0
    assert "[repeated_failing_action]" in d.stdout and "npm" in d.stdout
    assert "not a subscription charge" in d.stdout and "source-reported cost: not available" in d.stdout
    assert "TURNS (1)" in d.stdout and "65s" in d.stdout
    for sentinel in SENTINELS:
        assert sentinel not in d.stdout

    m = _run(["session", "--db", str(db), t.session_id[:8], "--set-customer", "acme", "--set-job", "refactor",
              "--findings-only"], cwd=tmp_path, home=home)
    assert "mapped explicitly" in m.stdout and "customer acme / job refactor (explicit)" in m.stdout

    f = _run(["findings", "--db", str(db), "--project", PROJECT], cwd=tmp_path, home=home)
    assert f.returncode == 0 and "1 shown" in f.stdout and "potential inefficiency" in f.stdout

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
    assert r.returncode == 0 and "0 transcript file(s) found" in r.stdout
    r2 = _run(["watch", "--db", str(tmp_path / "n.db"), "--source", "codex", "--once"], cwd=tmp_path, home=home)
    assert r2.returncode != 0 and "invalid choice" in r2.stderr
