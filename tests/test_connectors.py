"""Reversible agent configuration in isolated homes: connect, conflict, disconnect restores exactly."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from runpeek import connectors, telemetry
from runpeek.agents import codex
from runpeek.store import apply_schema, open_connection


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("RUNPEEK_GEMINI_HOME", str(tmp_path / "gemini"))
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))  # no claude/codex binaries: MCP registration is advisory
    codex._head_cache.clear()
    (tmp_path / "claude").mkdir()
    (tmp_path / "codex").mkdir()
    return tmp_path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_connection(tmp_path / "n.db")
    apply_schema(c)
    return c


def test_claude_code_connect_preserves_and_restores_settings(home: Path, conn: sqlite3.Connection) -> None:
    spath = home / "claude" / "settings.json"
    original = {"permissions": {"allow": ["Bash(ls:*)"]}, "env": {"MY_VAR": "1", "OTEL_LOG_USER_PROMPTS": "1"},
                "hooks": {}}
    spath.write_text(json.dumps(original))
    # an existing OTEL key means an exporter may be configured: refuse
    with pytest.raises(connectors.ConnectorError):
        connectors.connect_claude_code(conn, "tok", port=4327, register_mcp=False)
    original["env"].pop("OTEL_LOG_USER_PROMPTS")
    spath.write_text(json.dumps(original))
    res = connectors.connect_claude_code(conn, "tok", port=4327, register_mcp=True)
    assert res["telemetry"] == "configured" and res["backup"] and Path(res["backup"]).exists()
    assert "claude mcp add" in res["mcp"]  # no claude on PATH: the exact manual command is given
    now = json.loads(spath.read_text())
    assert now["env"]["MY_VAR"] == "1" and now["permissions"] == original["permissions"]
    assert now["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert now["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4327"
    assert now["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer tok"
    assert now["env"]["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json" and now["env"]["OTEL_LOG_USER_PROMPTS"] == "0"
    st = connectors.detect(conn)[0]
    assert st.source == "claude-code" and st.telemetry == "runpeek" and st.connected
    assert st.collecting == "connected, no usage received yet"
    # reconnect is idempotent (managed block recognised)
    connectors.connect_claude_code(conn, "tok2", port=4327, register_mcp=False)
    assert json.loads(spath.read_text())["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer tok2"
    res = connectors.disconnect_claude_code(conn)
    assert res["telemetry"] == "restored"
    restored = json.loads(spath.read_text())
    assert restored == original
    assert connectors.detect(conn)[0].telemetry == "not configured"
    assert connectors.load_state(conn) == {}


def test_claude_code_connect_from_no_settings_file(home: Path, conn: sqlite3.Connection) -> None:
    spath = home / "claude" / "settings.json"
    assert not spath.exists()
    connectors.connect_claude_code(conn, "tok", port=4400, register_mcp=False)
    assert json.loads(spath.read_text())["_runpeek"] == "managed-env"
    connectors.disconnect_claude_code(conn)
    assert json.loads(spath.read_text()) == {}


def test_codex_connect_appends_marked_block_and_disconnect_restores_bytes(home: Path,
                                                                        conn: sqlite3.Connection) -> None:
    cpath = home / "codex" / "config.toml"
    original = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\nargs = []\n\n[mcp_servers.other.env]\nA = "1"\n'
    cpath.write_text(original)
    res = connectors.connect_codex(conn, "tok", port=4327)
    assert res["telemetry"] == "configured" and res["mcp"] == "registered" and Path(res["backup"]).exists()
    text = cpath.read_text()
    assert text.startswith(original) and connectors.MARK_BEGIN in text and connectors.MARK_END in text
    assert 'endpoint = "http://127.0.0.1:4327/v1/logs", protocol = "json"' in text
    assert 'Authorization = "Bearer tok"' in text and "log_user_prompt = false" in text
    assert "[mcp_servers.runpeek]" in text and '"mcp"' in text
    st = connectors.detect(conn)[1]
    assert st.source == "codex" and st.telemetry == "runpeek" and st.mcp == "runpeek"
    assert connectors.connect_codex(conn, "tok", port=4327)["telemetry"] == "already configured"
    assert connectors.disconnect_codex(conn) == {"telemetry": "restored", "mcp": "removed"}
    assert cpath.read_text() == original
    assert connectors.detect(conn)[1].telemetry == "not configured"


def test_codex_existing_otel_is_never_overwritten(home: Path, conn: sqlite3.Connection) -> None:
    cpath = home / "codex" / "config.toml"
    cpath.write_text('[otel]\nexporter = "otlp-grpc"\n')
    with pytest.raises(connectors.ConnectorError):
        connectors.connect_codex(conn, "tok")
    assert cpath.read_text() == '[otel]\nexporter = "otlp-grpc"\n'
    st = connectors.detect(conn)[1]
    assert st.telemetry == "other exporter" and st.notes
    cpath.write_text('[mcp_servers.runpeek]\ncommand = "x"\n')
    with pytest.raises(connectors.ConnectorError):
        connectors.connect_codex(conn, "tok")


def test_gemini_is_detected_but_not_configured(home: Path, conn: sqlite3.Connection) -> None:
    st = connectors.detect(conn)[2]
    assert st.source == "gemini-cli" and st.telemetry == "unverified" and not st.installed
    with pytest.raises(connectors.ConnectorError):
        connectors.connect(conn, "gemini-cli", "tok")


def test_status_reports_collecting_after_telemetry_arrives(home: Path, conn: sqlite3.Connection) -> None:
    spath = home / "claude" / "settings.json"
    spath.write_text("{}")
    connectors.connect_claude_code(conn, "tok", register_mcp=False)
    ev = {"source": "claude-code", "provider": "anthropic", "session_id": "s1", "request_id": "req_1",
          "usage_id": "otel:req_1", "model": "claude-haiku-4-5-20251001", "at": "2026-09-11T10:00:00Z",
          "input_tokens": 10, "cache_read_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": None,
          "output_tokens": 2, "reasoning_tokens": None, "source_cost_nanos": 20_000, "turn_id": None,
          "query_source": "sdk", "source_version": "2.1.269"}
    telemetry.store_events(conn, [ev])
    st = connectors.detect(conn)[0]
    assert st.collecting.startswith("collecting (last event 2026-09-11T10:00:00Z")
    text = connectors.render_status(connectors.detect(conn), False)
    assert "NOT RUNNING" in text and "Claude Code" in text and "collecting (last event" in text
