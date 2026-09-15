"""Legacy names keep working after the rename to runpeek: NEMULAI_* env vars,
the old ./.nemulai/nemulai.db store (used in place, never copied or created),
nemulai-* attribution carriers, and the old bootstrap flag."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from runpeek.cli import DEFAULT_DB, LEGACY_DB, resolve_db
from runpeek.context import extract, inject, job
from runpeek.ids import env


def test_env_prefers_runpeek_then_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPEEK_DB", raising=False)
    monkeypatch.delenv("NEMULAI_DB", raising=False)
    assert env("DB") is None and env("DB", "x") == "x"
    monkeypatch.setenv("NEMULAI_DB", "/legacy.db")
    assert env("DB") == "/legacy.db"
    monkeypatch.setenv("RUNPEEK_DB", "/new.db")
    assert env("DB") == "/new.db"


def test_legacy_store_is_used_in_place_not_recreated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RUNPEEK_DB", raising=False)
    monkeypatch.delenv("NEMULAI_DB", raising=False)
    monkeypatch.setenv("RUNPEEK_HOME", str(tmp_path / "home"))
    assert resolve_db(None) == (tmp_path / "home" / "runpeek.db", None)  # nothing exists yet → user-level store
    from runpeek.cli import resolve_user_db

    assert resolve_user_db(None) == tmp_path / "home" / "runpeek.db" and resolve_user_db(None).is_absolute()
    LEGACY_DB.parent.mkdir()
    LEGACY_DB.write_bytes(b"")
    path, note = resolve_db(None)
    assert path == LEGACY_DB.resolve() and note and "legacy store" in note and "MIGRATION" in note
    DEFAULT_DB.parent.mkdir()
    DEFAULT_DB.write_bytes(b"")
    assert resolve_db(None) == (DEFAULT_DB.resolve(), None)  # once the new store exists it wins (absolute)
    assert resolve_db(None)[0].is_absolute()
    # the receiver, MCP server and setup ignore the per-project store: one shared user-level ledger
    assert resolve_user_db(None) == tmp_path / "home" / "runpeek.db"
    assert resolve_db("/explicit.db") == (Path("/explicit.db"), None)
    monkeypatch.setenv("NEMULAI_DB", "/env-legacy.db")
    path, note = resolve_db(None)
    assert path == Path("/env-legacy.db") and note and "NEMULAI_DB" in note


def test_legacy_carrier_keys_are_accepted() -> None:
    with job(customer="acme", job="j", k="v"):
        carrier = inject()
    assert "runpeek-customer" in carrier
    legacy = {k.replace("runpeek-", "nemulai-"): v for k, v in carrier.items()}
    with extract(legacy) as a:
        assert a is not None and a.customer == "acme" and a.job == "j" and dict(a.attributes) == {"k": "v"}


def test_legacy_bootstrap_flag_still_instruments(tmp_path: Path) -> None:
    boot = Path(sys.modules["runpeek"].__file__).parent / "_boot"  # type: ignore[arg-type]
    db = tmp_path / "legacy.db"
    envv = dict(os.environ)
    envv.pop("RUNPEEK_ENABLED", None)
    envv.update({"NEMULAI_ENABLED": "1", "NEMULAI_DB": str(db), "PYTHONPATH": str(boot)})
    r = subprocess.run([sys.executable, "-c", "import runpeek.bootstrap as b; print(b.current_store() is not None)"],
                       env=envv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "True", r.stderr
    assert db.exists()


def test_no_nemulai_import_package_is_shipped() -> None:
    # The legacy GPU agent owns the `nemulai` import name on PyPI; RunPeek must not collide with it.
    r = subprocess.run([sys.executable, "-c", "import nemulai"], capture_output=True, text=True)
    assert r.returncode != 0 or "runpeek" not in (r.stdout + r.stderr)
