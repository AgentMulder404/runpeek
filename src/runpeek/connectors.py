"""Reversible configuration of local coding agents.

"Connected" means RunPeek has pointed the agent's documented telemetry at the
local receiver and registered its MCP server. "Collecting" means usage events
have actually arrived. The two are reported separately.

Every change is recorded in the store (``meta.connector_state``) with the
previous values and a file backup, so ``disconnect`` restores exactly what was
there. Existing exporters are never overwritten: a conflict is reported and the
user decides.

Claude Code: ``~/.claude/settings.json`` ``env`` block (documented). The MCP
server is registered with ``claude mcp add -s user`` (documented CLI).
Codex: ``~/.codex/config.toml`` ``[otel]`` and ``[mcp_servers.runpeek]`` tables
(documented), appended inside marker comments. ``codex mcp add`` is not used
because it rewrites the whole file.
Gemini CLI: detected only. Its telemetry settings are documented but were not
verified with a real session (no credentials available), so RunPeek does not
configure it yet.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import claude_code, codex
from .ids import env, now_iso
from .telemetry import DEFAULT_PORT

MARK_BEGIN = "# >>> runpeek managed block (remove with: runpeek agents disconnect codex)"
MARK_END = "# <<< runpeek managed block"

CLAUDE_ENV_KEYS = ("CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_LOGS_EXPORTER", "OTEL_METRICS_EXPORTER",
                   "OTEL_TRACES_EXPORTER", "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT",
                   "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_LOGS_EXPORT_INTERVAL", "OTEL_LOG_USER_PROMPTS",
                   "OTEL_LOG_TOOL_DETAILS")


class ConnectorError(Exception):
    pass


@dataclass
class AgentStatus:
    source: str
    label: str
    installed: bool
    config_path: str
    records_found: int
    telemetry: str  # not configured | runpeek | other exporter | unverified
    mcp: str  # not registered | runpeek | unknown
    last_event_at: str | None = None
    sessions_with_telemetry: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def connected(self) -> bool:
        return self.telemetry == "runpeek"

    @property
    def collecting(self) -> str:
        if not self.connected:
            return "not connected"
        if not self.last_event_at:
            return "connected, no usage received yet"
        return f"collecting (last event {self.last_event_at})"


def runpeek_command() -> list[str]:
    """How an agent should launch the RunPeek MCP server."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    exe = shutil.which("runpeek")
    if exe:
        return [exe]
    return [sys.executable, "-m", "runpeek.cli"]


def claude_settings_path() -> Path:
    return claude_code.claude_home() / "settings.json"


def codex_config_path() -> Path:
    return codex.codex_home() / "config.toml"


def gemini_home() -> Path:
    return Path(env("GEMINI_HOME") or Path.home() / ".gemini")


# --------------------------------------------------------------------------- state


def load_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key = 'connector_state'").fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def save_state(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES ('connector_state', ?) ON CONFLICT(key) DO UPDATE SET"
                 " value = excluded.value", (json.dumps(state, sort_keys=True),))
    conn.commit()


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dst = path.with_name(f"{path.name}.runpeek-backup-{stamp}")
    shutil.copy2(path, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    return str(dst)


def _endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- detection


def detect(conn: sqlite3.Connection | None = None) -> list[AgentStatus]:
    out: list[AgentStatus] = []
    # Claude Code
    spath = claude_settings_path()
    records = len(claude_code.discover(None, all_projects=True))
    tele, notes = "not configured", []
    try:
        settings = json.loads(spath.read_text(encoding="utf-8")) if spath.exists() else {}
    except ValueError:
        settings, tele = {}, "not configured"
        notes.append("settings.json is not valid JSON; RunPeek will not modify it")
    envblock = settings.get("env") if isinstance(settings, dict) else None
    envblock = envblock if isinstance(envblock, dict) else {}
    if envblock.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1" or any(k.startswith("OTEL_") for k in envblock):
        endpoint = str(envblock.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "")
        tele = "runpeek" if settings.get("_runpeek") == "managed-env" and "127.0.0.1" in endpoint else "other exporter"
        if tele == "other exporter":
            notes.append(f"an OpenTelemetry exporter is already configured ({endpoint or 'endpoint unset'});"
                         " RunPeek will not overwrite it")
    mcp = "unknown"
    cj = claude_code.claude_home().parent / ".claude.json"
    try:
        servers = (json.loads(cj.read_text(encoding="utf-8")).get("mcpServers") or {}) if cj.exists() else {}
        mcp = "runpeek" if "runpeek" in servers else "not registered"
    except (ValueError, AttributeError, OSError):
        mcp = "unknown"
    out.append(AgentStatus("claude-code", "Claude Code", bool(shutil.which("claude")), str(spath), records, tele, mcp,
                           notes=notes))
    # Codex
    cpath = codex_config_path()
    records = len(codex.discover(None, all_projects=True))
    tele, notes, mcp = "not configured", [], "not registered"
    text = cpath.read_text(encoding="utf-8") if cpath.exists() else ""
    if MARK_BEGIN in text:
        tele, mcp = "runpeek", "runpeek"
    elif re.search(r"^\s*\[otel\]|^\s*otel\.", text, re.M):
        tele = "other exporter"
        notes.append("config.toml already has an [otel] section; RunPeek will not overwrite it")
    if re.search(r"^\s*\[mcp_servers\.runpeek\]", text, re.M):
        mcp = "runpeek"
    installed = bool(shutil.which("codex")) or (codex.codex_home() / "auth.json").exists()
    out.append(AgentStatus("codex", "Codex", installed, str(cpath), records, tele, mcp, notes=notes))
    # Gemini CLI: detected, not configured
    ghome = gemini_home()
    ginstalled = bool(shutil.which("gemini")) or (ghome / "settings.json").exists()
    out.append(AgentStatus("gemini-cli", "Gemini CLI", ginstalled, str(ghome / "settings.json"), 0, "unverified",
                           "not registered", notes=["telemetry documented but not verified with a real session;"
                                                    " RunPeek does not configure it yet"]))
    if conn is not None:
        for st in out:
            row = conn.execute(
                "SELECT MAX(telemetry_last_at) m, COUNT(*) n FROM agent_sessions WHERE source = ?"
                " AND telemetry_last_at IS NOT NULL", (st.source,)).fetchone()
            st.last_event_at, st.sessions_with_telemetry = row["m"], int(row["n"] or 0)
    return out


# --------------------------------------------------------------------------- claude code


def connect_claude_code(conn: sqlite3.Connection, token: str, *, port: int = DEFAULT_PORT,
                        register_mcp: bool = True) -> dict[str, Any]:
    spath = claude_settings_path()
    settings: dict[str, Any] = {}
    if spath.exists():
        try:
            loaded = json.loads(spath.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConnectorError(f"{spath} is not valid JSON; fix it before connecting") from exc
        if not isinstance(loaded, dict):
            raise ConnectorError(f"{spath} must contain a JSON object")
        settings = loaded
    envblock = settings.get("env")
    if envblock is None:
        envblock = {}
    if not isinstance(envblock, dict):
        raise ConnectorError("settings.json 'env' must be an object")
    managed = settings.get("_runpeek") == "managed-env"
    if not managed and any(k.startswith("OTEL_") or k == "CLAUDE_CODE_ENABLE_TELEMETRY" for k in envblock):
        raise ConnectorError("Claude Code already has an OpenTelemetry exporter configured in settings.json."
                             " RunPeek will not overwrite it. Remove it, or forward that exporter to"
                             f" {_endpoint(port)} yourself.")
    state = load_state(conn)
    if managed and state.get("claude-code", {}).get("previous_env") is not None:
        previous = state["claude-code"]["previous_env"]  # keep what was there before RunPeek, not our own values
    else:
        previous = {k: envblock.get(k) for k in CLAUDE_ENV_KEYS}
    backup = _backup(spath)
    envblock.update({
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": _endpoint(port),
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {token}",
        "OTEL_LOGS_EXPORT_INTERVAL": "2000",
        "OTEL_LOG_USER_PROMPTS": "0",
        "OTEL_LOG_TOOL_DETAILS": "0",
    })
    settings["env"] = envblock
    settings["_runpeek"] = "managed-env"
    spath.parent.mkdir(parents=True, exist_ok=True)
    spath.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    prior_mcp = state.get("claude-code", {}).get("mcp")
    state["claude-code"] = {"config": str(spath), "previous_env": previous, "backup": backup, "port": port,
                            "connected_at": now_iso(), "mcp": prior_mcp}
    result: dict[str, Any] = {"telemetry": "configured", "mcp": "skipped", "backup": backup}
    if register_mcp:
        result["mcp"] = _claude_mcp("add")
        state["claude-code"]["mcp"] = result["mcp"]
    save_state(conn, state)
    return result


def _claude_mcp(action: str) -> str:
    exe = shutil.which("claude")
    if not exe:
        return "claude CLI not on PATH; register manually: claude mcp add -s user runpeek -- " \
               + " ".join(runpeek_command() + ["mcp"])
    if action == "add":
        cmd = [exe, "mcp", "add", "-s", "user", "runpeek", "--", *runpeek_command(), "mcp"]
    else:
        cmd = [exe, "mcp", "remove", "-s", "user", "runpeek"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"failed: {type(exc).__name__}"
    if r.returncode != 0 and action == "remove" and "not found" in (r.stdout + r.stderr).lower():
        return "not registered"
    return "registered" if action == "add" and r.returncode == 0 else (
        "removed" if r.returncode == 0 else f"failed: {(r.stderr or r.stdout).strip()[:200]}")


def disconnect_claude_code(conn: sqlite3.Connection) -> dict[str, Any]:
    state = load_state(conn)
    entry = state.get("claude-code")
    spath = claude_settings_path()
    result: dict[str, Any] = {"telemetry": "not connected", "mcp": "skipped"}
    if entry and spath.exists():
        try:
            settings = json.loads(spath.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ConnectorError(f"{spath} is not valid JSON; restore it from {entry.get('backup')}") from exc
        envblock = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        for k, old in (entry.get("previous_env") or {}).items():
            if old is None:
                envblock.pop(k, None)
            else:
                envblock[k] = old
        if envblock:
            settings["env"] = envblock
        else:
            settings.pop("env", None)
        settings.pop("_runpeek", None)
        spath.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        result["telemetry"] = "restored"
    if entry and entry.get("mcp") == "registered":
        result["mcp"] = _claude_mcp("remove")
    state.pop("claude-code", None)
    save_state(conn, state)
    return result


# --------------------------------------------------------------------------- codex


def _codex_block(token: str, port: int) -> str:
    cmd = runpeek_command()
    args = json.dumps(cmd[1:] + ["mcp"])
    return "\n".join([
        MARK_BEGIN,
        "[otel]",
        f'exporter = {{ otlp-http = {{ endpoint = "{_endpoint(port)}/v1/logs", protocol = "json",'
        f' headers = {{ Authorization = "Bearer {token}" }} }} }}',
        "log_user_prompt = false",
        "",
        "[mcp_servers.runpeek]",
        f"command = {json.dumps(cmd[0])}",
        f"args = {args}",
        MARK_END,
        "",
    ])


def connect_codex(conn: sqlite3.Connection, token: str, *, port: int = DEFAULT_PORT) -> dict[str, Any]:
    cpath = codex_config_path()
    text = cpath.read_text(encoding="utf-8") if cpath.exists() else ""
    if MARK_BEGIN in text:
        return {"telemetry": "already configured", "mcp": "already registered", "backup": None}
    if re.search(r"^\s*\[otel\]|^\s*otel\.", text, re.M):
        raise ConnectorError("Codex already has an [otel] section in config.toml. RunPeek will not overwrite it;"
                             f" point that exporter at {_endpoint(port)}/v1/logs yourself if you want.")
    if re.search(r"^\s*\[mcp_servers\.runpeek\]", text, re.M):
        raise ConnectorError("config.toml already defines [mcp_servers.runpeek]; remove it first")
    backup = _backup(cpath)
    cpath.parent.mkdir(parents=True, exist_ok=True)
    new = text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + _codex_block(token, port)
    cpath.write_text(new, encoding="utf-8")
    try:
        os.chmod(cpath, 0o600)
    except OSError:
        pass
    state = load_state(conn)
    state["codex"] = {"config": str(cpath), "backup": backup, "port": port, "connected_at": now_iso()}
    save_state(conn, state)
    return {"telemetry": "configured", "mcp": "registered", "backup": backup,
            "note": "Codex reads config.toml at start; restart Codex Desktop or the CLI to apply."}


def disconnect_codex(conn: sqlite3.Connection) -> dict[str, Any]:
    cpath = codex_config_path()
    result: dict[str, Any] = {"telemetry": "not connected", "mcp": "skipped"}
    if cpath.exists():
        text = cpath.read_text(encoding="utf-8")
        pattern = re.compile(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", re.S)
        if pattern.search(text):
            cpath.write_text(pattern.sub("", text).rstrip("\n") + "\n" if text.strip() else "", encoding="utf-8")
            result = {"telemetry": "restored", "mcp": "removed"}
    state = load_state(conn)
    state.pop("codex", None)
    save_state(conn, state)
    return result


# --------------------------------------------------------------------------- dispatch


def connect(conn: sqlite3.Connection, source: str, token: str, *, port: int = DEFAULT_PORT) -> dict[str, Any]:
    if source == "claude-code":
        return connect_claude_code(conn, token, port=port)
    if source == "codex":
        return connect_codex(conn, token, port=port)
    if source == "gemini-cli":
        raise ConnectorError("Gemini CLI telemetry has not been verified with a real session; not configured."
                             " See docs/EVIDENCE_MATRIX.md.")
    raise ConnectorError(f"unknown agent {source!r}")


def disconnect(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    if source == "claude-code":
        return disconnect_claude_code(conn)
    if source == "codex":
        return disconnect_codex(conn)
    raise ConnectorError(f"unknown agent {source!r}")


def render_status(statuses: list[AgentStatus], receiver_ok: bool | None) -> str:
    L = ["AGENT CONNECTIONS", ""]
    if receiver_ok is None:
        L.append("Receiver: not checked")
    else:
        L.append("Receiver: " + ("running" if receiver_ok else "NOT RUNNING — usage sent now is lost; start it with"
                                                                " `runpeek telemetry serve` or `runpeek setup`"))
    L.append("")
    L.append(f"  {'Agent':<12}{'Installed':<11}{'Records':>8}  {'Telemetry':<16}{'MCP':<16}Collecting")
    for s in statuses:
        L.append(f"  {s.label:<12}{'yes' if s.installed else 'no':<11}{s.records_found:>8}  {s.telemetry:<16}"
                 f"{s.mcp:<16}{s.collecting}")
    notes = [(s.label, n) for s in statuses for n in s.notes]
    if notes:
        L.append("")
        for label, n in notes:
            L.append(f"  {label}: {n}")
    L.append("")
    L.append("Connected = telemetry points at the local receiver. Collecting = usage events have actually arrived.")
    L.append("Records = session files readable by the transcript adapters (backfill and compatibility).")
    return "\n".join(L)
