from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    e = dict(os.environ)
    e.pop("NEMULAI_ENABLED", None)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, "-m", "nemulai.cli", *args], cwd=cwd, env=e,
                          capture_output=True, text=True, timeout=120)


def test_run_example_then_summary_and_export(tmp_path: Path) -> None:
    db = tmp_path / "n.db"
    r = _run(["run", "--db", str(db), "--", sys.executable, str(EXAMPLES / "basic.py")], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "support assistant (offline demo)" in r.stdout
    assert "KNOWN ESTIMATED COST" in r.stdout
    assert "acme" in r.stdout and "(unattributed)" in r.stdout
    assert 'model not in rate card: "acme-preview-1" (1)' in r.stdout
    assert "provider_error 2" in r.stdout
    assert "ended cleanly" in r.stdout

    s = _run(["summary", "--db", str(db)], cwd=tmp_path)
    assert s.returncode == 0 and "KNOWN ESTIMATED COST" in s.stdout

    ev = _run(["events", "--db", str(db), "--last", "3"], cwd=tmp_path)
    assert ev.returncode == 0 and ev.stdout.count("\n") == 4

    out = tmp_path / "export.jsonl"
    x = _run(["export", "--db", str(db), "--format", "jsonl", "--out", str(out)], cwd=tmp_path)
    assert x.returncode == 0
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    kinds = {r["record"] for r in recs}
    assert {"runs", "operations", "attempts", "source_observations", "charges", "cost_estimates",
            "identifiers", "health_events", "spans"} <= kinds
    est = [r for r in recs if r["record"] == "cost_estimates" and r["amount_nanos"] is not None]
    assert est and all(r["amount_usd"] == f"{r['amount_nanos'] // 10**9}.{r['amount_nanos'] % 10**9:09d}" for r in est)
    assert not any("messages" in r or "content" in r for r in recs)  # no request bodies, ever

    rp = _run(["reprice", "--db", str(db), "--pin", "openai-list@2025-08-01"], cwd=tmp_path)
    assert rp.returncode == 0 and "pinned:openai-list@2025-08-01" in rp.stdout
    rp2 = _run(["reprice", "--db", str(db), "--pin", "openai-list@2025-08-01"], cwd=tmp_path)
    assert "0 estimate revisions created" in rp2.stdout


def test_application_exit_status_is_preserved(tmp_path: Path) -> None:
    r = _run(["run", "--db", str(tmp_path / "n.db"), "--no-summary", "--", sys.executable, "-c",
              "import sys; sys.exit(3)"], cwd=tmp_path)
    assert r.returncode == 3


def test_application_signal_death_maps_to_128_plus(tmp_path: Path) -> None:
    r = _run(["run", "--db", str(tmp_path / "n.db"), "--no-summary", "--", sys.executable, "-c",
              "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"], cwd=tmp_path)
    assert r.returncode == 128 + 15
    import sqlite3

    assert sqlite3.connect(tmp_path / "n.db").execute("SELECT app_exit_status FROM runs").fetchone()[0] == -15


def test_uninstrumentable_command_runs_and_warns(tmp_path: Path) -> None:
    r = _run(["run", "--db", str(tmp_path / "none.db"), "--", "true"], cwd=tmp_path)
    assert r.returncode == 0
    assert "no store was created" in r.stderr


def test_existing_sitecustomize_is_chained(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    marker = tmp_path / "marker.txt"
    (other / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    db = tmp_path / "n.db"
    r = _run(["run", "--db", str(db), "--no-summary", "--", sys.executable, "-c", "print('app ok')"],
             cwd=tmp_path, env={"PYTHONPATH": str(other)})
    assert r.returncode == 0 and "app ok" in r.stdout, r.stderr
    assert marker.read_text() == "ran"
    assert db.exists()


def test_site_disabled_interpreter_is_not_covered(tmp_path: Path) -> None:
    db = tmp_path / "n.db"
    r = _run(["run", "--db", str(db), "--no-summary", "--", sys.executable, "-S", "-c", "print('x')"],
             cwd=tmp_path)
    assert r.returncode == 0
    assert not db.exists()  # documented: -S skips site, so the bootstrap never runs


def test_crash_leaves_readable_store_and_unclean_run(tmp_path: Path) -> None:
    db = tmp_path / "n.db"
    app = (
        f"import os, sys; sys.path.insert(0, {str(EXAMPLES)!r})\n"
        "from _mock_openai import chat_json, scripted_client\n"
        "import nemulai\n"
        "c = scripted_client([{'json': chat_json(), 'request_id': 'r'}])\n"
        "with nemulai.job(customer='acme'):\n"
        "    c.chat.completions.create(model='gpt-4.1-mini', messages=[{'role':'user','content':'x'}])\n"
        "import time; time.sleep(0.3)\n"  # let the writer commit the attempt
        "os._exit(9)\n"  # no atexit: run never closes
    )
    r = _run(["run", "--db", str(db), "--", sys.executable, "-c", app], cwd=tmp_path)
    assert r.returncode == 9
    assert "telemetry: DID NOT END CLEANLY" in r.stdout
    assert "application exit: 9 (failed)" in r.stdout
    assert "persisted rows" in r.stdout and "final counters unavailable" in r.stdout
    assert "records 0" not in r.stdout
    assert "customer='acme'" not in r.stdout  # the -c program is never displayed
    import sqlite3

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT ended_at FROM runs").fetchone()[0] is None


def test_command_description_redacts_code_and_secrets() -> None:
    from nemulai.cli import describe_command

    assert describe_command(["python", "app.py", "--verbose"]) == "python app.py --verbose"
    assert describe_command(["python", "-c", "print(open('secret').read())"]) == "python -c <redacted>"
    described = describe_command(["python", "-m", "pkg.mod", "--api-key=sk-live-123"])
    assert described == "python -m pkg.mod --api-key=<redacted>"
    assert describe_command(["tool", "--token", "abc", "run"]) == "tool --token <redacted> run"
    assert describe_command(["python", "x.py", "two words"]) == "python x.py <redacted>"
    long = describe_command(["python"] + ["a" * 50] * 10)
    assert len(long) <= 200 and long.endswith("…")


def test_inline_program_is_not_stored_and_statuses_are_separate(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "n.db"
    r = _run(["run", "--db", str(db), "--", sys.executable, "-c",
              "SECRET_PROMPT = 'do not leak me'\nimport sys; sys.exit(2)"], cwd=tmp_path)
    assert r.returncode == 2
    assert "SECRET_PROMPT" not in r.stdout
    assert "application exit: 2 (failed)" in r.stdout and "telemetry: ended cleanly" in r.stdout
    conn = sqlite3.connect(db)
    cmd, app_rc = conn.execute("SELECT command, app_exit_status FROM runs").fetchone()
    assert cmd.endswith("-c <redacted>") and "SECRET_PROMPT" not in cmd
    assert app_rc == 2


@pytest.mark.parametrize("flag", ["--version"])
def test_version(flag: str, tmp_path: Path) -> None:
    r = _run([flag], cwd=tmp_path)
    assert r.returncode == 0 and "nemulai 0.1.0" in r.stdout
