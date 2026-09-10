"""Claude Code source adapter — EXPERIMENTAL, versioned.

What is documented and what is not (checked 2026-09-09 against Claude Code
2.1.258 and https://code.claude.com/docs/en/hooks):

* Documented: the transcript file's *location* — hooks receive
  ``transcript_path`` under ``~/.claude/projects/<encoded cwd>/<session>.jsonl``
  — and ``session_id`` / ``prompt_id`` / ``tool_use_id`` identifiers.
* Not documented: the transcript's line format. This adapter parses it, is
  labelled experimental, and is gated on the writer ``version`` field each
  entry carries. Files written by 2.1.202–2.1.257 were inspected structurally;
  ``SUPPORTED_MAJOR_MINOR`` says what the parser was built against. Anything
  else is ingested best-effort and the session is marked ``unknown_version``.
* Usage: each ``assistant`` entry carries ``message.usage`` for one API request
  (``input_tokens``, ``cache_creation_input_tokens`` with a 5m/1h breakdown,
  ``cache_read_input_tokens``, ``output_tokens``). Streaming writes several
  entries per API response, all sharing ``message.id`` with identical usage —
  usage is therefore keyed by ``message.id`` and counted once.
* Not available in transcripts: cost. Claude Code reports ``cost_usd`` only
  through its documented OpenTelemetry export, which this adapter does not
  consume. ``source_cost_nanos`` is always NULL for this source.

Privacy: prompts, tool inputs, tool outputs and file contents are read into
memory only as far as needed to normalise an action, and never persisted.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..ids import env
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

SOURCE = "claude-code"
PROVIDER = "anthropic"
SUPPORTED_MAJOR_MINOR = ("2.1",)
INSPECTED_WRITER_VERSIONS = "2.1.202–2.1.257 (CLI 2.1.258 installed)"

_FILE_TOOLS = {"Read": "read", "Edit": "edit", "Write": "write", "MultiEdit": "edit", "NotebookEdit": "edit"}
_SEARCH_TOOLS = {"Grep", "Glob", "ToolSearch"}
_WEB_TOOLS = {"WebFetch", "WebSearch"}


def claude_home() -> Path:
    return Path(env("CLAUDE_HOME") or Path.home() / ".claude")


def encode_project_path(project_path: str | Path) -> str:
    """``/Users/x/my proj`` → ``-Users-x-my-proj`` (every non-alphanumeric → '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project_path))


def project_dir(project_path: str | Path) -> Path:
    return claude_home() / "projects" / encode_project_path(project_path)


def _project_path_from_index(pdir: Path) -> str | None:
    idx = pdir / "sessions-index.json"
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
        for e in data.get("entries", []):
            if e.get("projectPath"):
                return str(e["projectPath"])
    except (OSError, ValueError, AttributeError):
        pass
    return None


def discover(project_path: str | Path | None, *, all_projects: bool = False) -> list[TranscriptFile]:
    """Transcripts for one project (default) or every project under ~/.claude."""
    root = claude_home() / "projects"
    if not root.is_dir():
        return []
    pdirs: list[tuple[Path, str | None]]
    if all_projects:
        pdirs = [(d, _project_path_from_index(d)) for d in sorted(root.iterdir()) if d.is_dir()]
    else:
        assert project_path is not None
        pdirs = [(project_dir(project_path), str(project_path))]
    out: list[TranscriptFile] = []
    for pdir, ppath in pdirs:
        if not pdir.is_dir():
            continue
        for f in sorted(pdir.glob("*.jsonl")):
            out.append(TranscriptFile(path=f, session_id=f.stem, project_path=ppath))
            sub = pdir / f.stem / "subagents"
            if sub.is_dir():
                for sf in sorted(sub.glob("*.jsonl")):
                    out.append(TranscriptFile(path=sf, session_id=f"{f.stem}/{sf.stem}", project_path=ppath,
                                              parent_session_id=f.stem))
    return out


def version_supported(version: str | None) -> bool:
    if not version:
        return False
    parts = version.split(".")
    return ".".join(parts[:2]) in SUPPORTED_MAJOR_MINOR


def _rel_target(path_value: Any, cwd: str | None) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    p = Path(path_value)
    if cwd and p.is_absolute():
        try:
            return str(p.relative_to(cwd))
        except ValueError:
            return f"<outside-project>/{p.name}"
    return str(p)


def _host(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#@]*@)?([^/?#:]+)", url, re.I)
    return m.group(2).lower() if m else None


def _program(command: Any) -> str | None:
    if not isinstance(command, str):
        return None
    toks = command.strip().split()
    # skip leading env assignments and common wrappers
    for t in toks:
        if "=" in t and not t.startswith("-"):
            continue
        if t in ("sudo", "env", "nohup", "time", "exec"):
            continue
        return os.path.basename(t)[:64]
    return None


def normalise_tool_use(name: str, inp: dict[str, Any], cwd: str | None) -> tuple[str, str | None, dict[str, Any]]:
    """(action_kind, allowlisted target, fingerprint input). The fingerprint
    input may contain content; it is hashed by the ingestor and dropped."""
    if name in _FILE_TOOLS:
        kind = _FILE_TOOLS[name]
        target = _rel_target(inp.get("file_path") or inp.get("notebook_path"), cwd)
        fp: dict[str, Any] = {"tool": name, "path": target}
        if name == "Read":
            fp.update(offset=inp.get("offset"), limit=inp.get("limit"))
        elif name == "Edit":
            fp.update(old=inp.get("old_string"), new=inp.get("new_string"))
        elif name == "Write":
            fp.update(content=inp.get("content"))
        return kind, target, fp
    if name == "Bash":
        cmd = inp.get("command")
        norm = re.sub(r"\s+", " ", cmd.strip()) if isinstance(cmd, str) else None
        return "bash", _program(cmd), {"tool": name, "command": norm}
    if name in _SEARCH_TOOLS:
        keys = ("pattern", "path", "glob", "query")
        return "search", _rel_target(inp.get("path"), cwd), {"tool": name, **{k: inp.get(k) for k in keys}}
    if name in _WEB_TOOLS:
        return "web", _host(inp.get("url")), {"tool": name, "url": inp.get("url"), "query": inp.get("query")}
    if name.startswith("mcp__"):
        return "mcp", None, {"tool": name, "input": inp}
    return "other", None, {"tool": name, "input": inp}


class Parser:
    """Stateful per-transcript parser. Feed it one JSON line at a time."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # A subagent transcript reuses its parent's promptId; turn ids are
        # namespaced per session so the two never collide in the store.
        self._turn_ns = session_id.split("/")[-1][:8] if "/" in session_id else None
        self.cwd: str | None = None
        self.version: str | None = None
        self.current_turn: str | None = None
        self._seen_turns: set[str] = set()

    def parse_line(self, line: str, line_no: int) -> Iterator[Event]:
        try:
            e = json.loads(line)
        except ValueError as exc:
            yield Unparseable(line_no, f"invalid JSON: {type(exc).__name__}")
            return
        if not isinstance(e, dict):
            yield Unparseable(line_no, "entry is not an object")
            return
        t = e.get("type")
        ts = e.get("timestamp") if isinstance(e.get("timestamp"), str) else None
        if e.get("version") and not self.version:
            self.version = str(e["version"])
            cwd = e.get("cwd") if isinstance(e.get("cwd"), str) else None
            yield SessionInfo(self.session_id, self.version, cwd, ts)
        if isinstance(e.get("cwd"), str) and not self.cwd:
            self.cwd = e["cwd"]
        if t == "user":
            yield from self._user(e, ts)
        elif t == "assistant":
            yield from self._assistant(e, ts)
        elif t == "system" and e.get("subtype") == "turn_duration":
            d = e.get("durationMs")
            yield TurnDurationEvent(int(d) if isinstance(d, (int, float)) else None, ts)
        # mode / permission-mode / attachment / file-history-* / ai-title / last-prompt: no economic content

    def _user(self, e: dict[str, Any], ts: str | None) -> Iterator[Event]:
        raw = e.get("message")
        m: dict[str, Any] = raw if isinstance(raw, dict) else {}
        content = m.get("content")
        blocks = content if isinstance(content, list) else []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            for b in results:
                tid = b.get("tool_use_id")
                if isinstance(tid, str):
                    ie = b.get("is_error")
                    yield ToolResultEvent(tid, ts, bool(ie) if ie is not None else False)
            return
        # A user entry that is not a tool result is a new turn.
        pid = e.get("promptId") if isinstance(e.get("promptId"), str) else None
        turn_id = pid or f"turn_{e.get('uuid') or ts or 'unknown'}"
        if self._turn_ns:
            turn_id = f"{turn_id}@{self._turn_ns}"
        if turn_id not in self._seen_turns:
            self._seen_turns.add(turn_id)
            self.current_turn = turn_id
            yield TurnStartEvent(turn_id, ts)

    def _assistant(self, e: dict[str, Any], ts: str | None) -> Iterator[Event]:
        raw = e.get("message")
        m: dict[str, Any] = raw if isinstance(raw, dict) else {}
        mid = m.get("id") if isinstance(m.get("id"), str) else None
        u = m.get("usage")
        if mid and isinstance(u, dict):
            raw_cc, raw_stu = u.get("cache_creation"), u.get("server_tool_use")
            cc: dict[str, Any] = raw_cc if isinstance(raw_cc, dict) else {}
            stu: dict[str, Any] = raw_stu if isinstance(raw_stu, dict) else {}
            yield UsageEvent(
                usage_id=mid,
                request_id=e.get("requestId") if isinstance(e.get("requestId"), str) else None,
                model=m.get("model") if isinstance(m.get("model"), str) else None,
                at=ts,
                input_tokens=_int(u.get("input_tokens")),
                cache_write_5m_tokens=(_int(cc.get("ephemeral_5m_input_tokens")) if cc
                                       else _int(u.get("cache_creation_input_tokens"))),
                cache_write_1h_tokens=_int(cc.get("ephemeral_1h_input_tokens")) if cc else None,
                cache_read_tokens=_int(u.get("cache_read_input_tokens")),
                output_tokens=_int(u.get("output_tokens")),
                web_search_requests=_int(stu.get("web_search_requests")),
                web_fetch_requests=_int(stu.get("web_fetch_requests")),
            )
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str):
                name = str(b.get("name") or "unknown")
                raw_inp = b.get("input")
                inp: dict[str, Any] = raw_inp if isinstance(raw_inp, dict) else {}
                kind, target, fp = normalise_tool_use(name, inp, self.cwd)
                yield ToolUseEvent(b["id"], ts, name, kind, target, fp)


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
