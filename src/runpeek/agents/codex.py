"""Codex source adapter — EXPERIMENTAL, versioned.

Evidence base (structural inspection on 2026-09-10 of 38 rollout files written
by Codex CLI 0.130.0-alpha.5 … 0.153.1 on this machine; keys and counts only,
no content). No public documentation of the rollout format was found, so every
statement below is "verified on disk", not "documented".

* Location: ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``. The
  file's uuid is the thread id. ``session_meta.payload.session_id`` is the
  *root* session for subagent files, so identity comes from the file name.
* Subagents (0.153+): ``session_meta.thread_source == "subagent"`` with
  ``parent_thread_id``, ``forked_from_id`` and
  ``subagent_history_start_ordinal``. A subagent file starts with a copy of the
  parent's history up to that ordinal; no ``token_count`` was observed inside
  the copied prefix, and the subagent's cumulative totals restart at zero
  (verified: first total == first last). Parent and child usage are therefore
  independent; the adapter still skips usage below the start ordinal.
* Turns: ``event_msg`` ``task_started`` (``turn_id``, ``started_at`` epoch s),
  ``task_complete``, ``turn_aborted`` (``duration_ms``). ``turn_context``
  carries the turn's ``model``; ``thread_settings_applied`` may change it.
* Tool calls: ``response_item`` ``custom_tool_call`` (``exec``: input is the
  shell command string; ``apply_patch``: a patch text) and ``function_call``
  (``exec_command`` with JSON ``cmd``; ``view_image``; ``spawn_agent`` …), both
  keyed by ``call_id``; results in ``*_output``. Errors: ``exec_command`` output
  is JSON with ``metadata.exit_code``; some outputs start with ``Exit code: N``;
  otherwise unknown (NULL). MCP calls and web searches mostly appear only as
  ``item_completed`` items (``McpToolCall`` with ``status``/``error``,
  ``WebSearch``) and are recorded from there when their id is not already a
  known ``call_id``.
* Usage: ``event_msg`` ``token_count`` carries ``info.last_token_usage`` and
  ``info.total_token_usage`` (cumulative). **``last_token_usage`` is not safe to
  sum**: 112 of 3,847 events repeated the previous value with the total
  unchanged (typically after ``turn_aborted``). The adapter emits one usage
  event per *increase in the cumulative total* and ignores events whose total
  did not move. Totals never decreased in inspected data; a decrease is handled
  as a reset (fall back to ``last_token_usage``) and counted.
  0.153+ also writes ``token_usage_record`` with a ``response_id``; it is used
  only to attach that id to the next usage event, never as a second source.
  ``input_tokens`` includes ``cached_input_tokens``; ``output_tokens`` includes
  ``reasoning_output_tokens`` (OpenAI semantics). Normalised here to uncached
  input + cache read; reasoning kept as information.
* Cost: not reported anywhere in the records (rate-limit percentages only).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
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

SOURCE = "codex"
PROVIDER = "openai"
LABEL = "Codex"
RECORDS_LABEL = "Codex session records"
INSPECTED_CLI_VERSIONS = "0.130.0-alpha.5 – 0.153.1"
SUPPORTED_MINOR_RANGE = (130, 153)  # 0.<minor>; outside → unknown_version, parsed best-effort

_FILE_RE = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<id>[0-9a-fA-F-]{20,})\.jsonl$")
_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
               "reasoning_output_tokens")
_EXIT_RE = re.compile(r"(?i)\bexit(?:ed)?(?: with)? code[:\s]+(-?\d+)")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)

_head_cache: dict[str, dict[str, Any]] = {}


def codex_home() -> Path:
    return Path(env("CODEX_HOME") or Path.home() / ".codex")


def version_supported(version: str | None) -> bool:
    if not version:
        return False
    m = re.match(r"^0\.(\d+)", version)
    if not m:
        return False
    lo, hi = SUPPORTED_MINOR_RANGE
    return lo <= int(m.group(1)) <= hi


def _head(path: Path) -> dict[str, Any]:
    """The allowlisted part of the first line (session_meta). Cached per path."""
    key = str(path)
    if key in _head_cache:
        return _head_cache[key]
    info: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            line = fh.readline()
        e = json.loads(line)
        p = e.get("payload") if isinstance(e, dict) else None
        if isinstance(e, dict) and e.get("type") == "session_meta" and isinstance(p, dict):
            info = {
                "cwd": p.get("cwd") if isinstance(p.get("cwd"), str) else None,
                "thread_source": p.get("thread_source"),
                "parent_thread_id": p.get("parent_thread_id") if isinstance(p.get("parent_thread_id"), str) else None,
            }
    except (OSError, ValueError):
        pass
    if info:
        _head_cache[key] = info
    return info


def _norm(p: str | Path) -> str:
    return os.path.normpath(str(p))


def discover(project_path: str | Path | None, *, all_projects: bool = False) -> list[TranscriptFile]:
    """Rollout files for one project (by ``session_meta.cwd``) or every project."""
    root = codex_home() / "sessions"
    if not root.is_dir():
        return []
    want = None if all_projects else _norm(project_path or "")
    out: list[TranscriptFile] = []
    for f in sorted(root.glob("*/*/*/rollout-*.jsonl")):
        m = _FILE_RE.match(f.name)
        if not m:
            continue
        head = _head(f)
        cwd = head.get("cwd")
        if want is not None and (cwd is None or _norm(cwd) != want):
            continue
        parent = head.get("parent_thread_id") if head.get("thread_source") == "subagent" else None
        out.append(TranscriptFile(path=f, session_id=m.group("id"), project_path=cwd, parent_session_id=parent,
                                  source=SOURCE))
    return out


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


def _program(command: Any) -> str | None:
    if not isinstance(command, str):
        return None
    for t in command.strip().split():
        if "=" in t and not t.startswith("-"):
            continue
        if t in ("sudo", "env", "nohup", "time", "exec", "bash", "sh", "zsh", "-lc", "-c"):
            continue
        return os.path.basename(t)[:64]
    return None


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _epoch_iso(v: Any) -> str | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ts_ms(s: str | None) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.timestamp() * 1000.0
    except ValueError:
        return None


def normalise_tool_call(name: str, raw_input: Any, cwd: str | None) -> tuple[str, str | None, dict[str, Any]]:
    """(action_kind, allowlisted target, fingerprint input). The fingerprint
    input may contain content; the ingestor hashes and drops it."""
    if name in ("exec", "shell", "exec_command", "shell_command", "local_shell"):
        cmd: Any = raw_input
        if isinstance(raw_input, str) and name != "exec" and raw_input.lstrip().startswith("{"):
            try:
                cmd = json.loads(raw_input).get("cmd")
            except ValueError:
                cmd = None
        elif isinstance(raw_input, dict):
            cmd = raw_input.get("cmd") or raw_input.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        norm = re.sub(r"\s+", " ", cmd.strip()) if isinstance(cmd, str) else None
        return "bash", _program(cmd), {"tool": name, "command": norm}
    if name == "apply_patch":
        text = raw_input if isinstance(raw_input, str) else json.dumps(raw_input, sort_keys=True, default=str)
        files = _PATCH_FILE_RE.findall(text)
        target = _rel_target(files[0], cwd) if files else None
        return "edit", target, {"tool": name, "patch": text}
    if name == "view_image":
        path = None
        try:
            args = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
            path = args.get("path") if isinstance(args, dict) else None
        except ValueError:
            pass
        return "read", _rel_target(path, cwd), {"tool": name, "path": path}
    if name in ("web_search", "web_search_call"):
        return "web", None, {"tool": name, "input": raw_input}
    return "other", None, {"tool": name, "input": raw_input}


def _error_from_output(output: Any) -> bool | None:
    if isinstance(output, dict):
        meta = output.get("metadata")
        if isinstance(meta, dict) and _int(meta.get("exit_code")) is not None:
            return int(meta["exit_code"]) != 0
        return None
    if not isinstance(output, str):
        return None
    s = output.lstrip()
    if s.startswith("{"):
        try:
            return _error_from_output(json.loads(s))
        except ValueError:
            return None
    m = _EXIT_RE.search(s[:200])
    if m:
        return int(m.group(1)) != 0
    return None


class Parser:
    """Stateful per-rollout parser. Feed it one JSON line at a time."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.cwd: str | None = None
        self.version: str | None = None
        self.git_branch: str | None = None
        self.repository_url: str | None = None
        self.provider = PROVIDER
        self.current_turn: str | None = None
        self.model: str | None = None
        self._seen_turns: set[str] = set()
        self._turn_started_ms: dict[str, float] = {}
        self._prev_total: dict[str, int] | None = None
        self._pending_request_id: str | None = None
        self._start_ordinal: int | None = None
        self._call_ids: set[str] = set()
        # usage-consistency counters, stored on the session (JSON) for the coverage report
        self.counters: dict[str, int] = {"stale_usage_repeats": 0, "total_resets": 0, "usage_without_model": 0,
                                         "usage_in_copied_prefix": 0}

    # ------------------------------------------------------------------ lines

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
        ordinal = _int(e.get("ordinal"))
        raw_p = e.get("payload")
        p: dict[str, Any] = raw_p if isinstance(raw_p, dict) else {}
        if t == "session_meta":
            yield from self._session_meta(p, ts)
        elif t == "turn_context":
            if isinstance(p.get("model"), str):
                self.model = p["model"]
            tid = p.get("turn_id")
            if isinstance(tid, str):
                yield from self._start_turn(tid, ts)
        elif t == "event_msg":
            yield from self._event_msg(p, ts, ordinal)
        elif t == "response_item":
            yield from self._response_item(p, ts)
        elif t == "token_usage_record":
            rid = p.get("response_id")
            if isinstance(rid, str):
                self._pending_request_id = rid
        # compacted / world_state / inter_agent_communication_metadata: no economic content

    def _session_meta(self, p: dict[str, Any], ts: str | None) -> Iterator[Event]:
        if self.version:
            return  # subagent files repeat session_meta; the first one wins
        self.version = str(p.get("cli_version")) if p.get("cli_version") else None
        self.cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else None
        git = p.get("git")
        if isinstance(git, dict):
            self.git_branch = git.get("branch") if isinstance(git.get("branch"), str) else None
            self.repository_url = git.get("repository_url") if isinstance(git.get("repository_url"), str) else None
        self._start_ordinal = _int(p.get("subagent_history_start_ordinal"))
        yield SessionInfo(self.session_id, self.version, self.cwd, ts, git_branch=self.git_branch,
                          repository_url=self.repository_url)

    def _start_turn(self, turn_id: str, ts: str | None) -> Iterator[Event]:
        self.current_turn = turn_id
        if turn_id in self._seen_turns:
            return
        self._seen_turns.add(turn_id)
        yield TurnStartEvent(turn_id, ts)

    def _event_msg(self, p: dict[str, Any], ts: str | None, ordinal: int | None) -> Iterator[Event]:
        kind = p.get("type")
        if kind == "task_started":
            tid = p.get("turn_id")
            if isinstance(tid, str):
                started = p.get("started_at")
                if isinstance(started, (int, float)) and not isinstance(started, bool):
                    self._turn_started_ms[tid] = float(started) * 1000.0
                yield from self._start_turn(tid, ts)
        elif kind in ("task_complete", "turn_aborted"):
            tid = p.get("turn_id") if isinstance(p.get("turn_id"), str) else self.current_turn
            dur = _int(p.get("duration_ms"))
            if dur is None and tid in self._turn_started_ms:
                end = _ts_ms(ts)
                if end is not None:
                    dur = max(int(end - self._turn_started_ms[tid]), 0)
            yield TurnDurationEvent(dur, ts, turn_id=tid)
        elif kind == "thread_settings_applied":
            settings = p.get("thread_settings")
            if isinstance(settings, dict) and isinstance(settings.get("model"), str):
                self.model = settings["model"]
        elif kind == "token_count":
            yield from self._token_count(p, ts, ordinal)
        elif kind == "item_completed":
            yield from self._item(p, ts)

    def _token_count(self, p: dict[str, Any], ts: str | None, ordinal: int | None) -> Iterator[Event]:
        info = p.get("info")
        if not isinstance(info, dict):
            return  # rate-limit-only event, no usage
        total = info.get("total_token_usage")
        last = info.get("last_token_usage")
        if not isinstance(total, dict):
            return
        if self._start_ordinal is not None and ordinal is not None and ordinal < self._start_ordinal:
            self.counters["usage_in_copied_prefix"] += 1
            return
        cur = {k: (_int(total.get(k)) or 0) for k in _USAGE_KEYS}
        prev = self._prev_total
        delta = cur if prev is None else {k: cur[k] - prev[k] for k in _USAGE_KEYS}
        self._prev_total = cur
        if all(v == 0 for v in delta.values()):
            self.counters["stale_usage_repeats"] += 1
            return
        provenance = "provider_reported"
        if any(v < 0 for v in delta.values()):
            # never observed; a reset of the cumulative counter. The last-usage block is the best evidence.
            self.counters["total_resets"] += 1
            delta = {k: (_int(last.get(k)) or 0) for k in _USAGE_KEYS} if isinstance(last, dict) else cur
            provenance = "provider_reported_after_reset"
        if self.model is None:
            self.counters["usage_without_model"] += 1
        cached = delta["cached_input_tokens"]
        uncached = max(delta["input_tokens"] - cached, 0)
        rid = self._pending_request_id
        self._pending_request_id = None
        usage_id = f"{self.session_id}:{ordinal if ordinal is not None else 'l' + str(id(p))}"
        yield UsageEvent(
            usage_id=usage_id,
            request_id=rid,
            model=self.model,
            at=ts,
            input_tokens=uncached,
            cache_write_5m_tokens=delta["cache_write_input_tokens"] or None,
            cache_write_1h_tokens=None,
            cache_read_tokens=cached,
            output_tokens=delta["output_tokens"],
            web_search_requests=None,
            web_fetch_requests=None,
            usage_kind="per_request",
            provenance=provenance,
            provider=PROVIDER,
            reasoning_tokens=delta["reasoning_output_tokens"],
            ordinal=ordinal,
        )

    def _response_item(self, p: dict[str, Any], ts: str | None) -> Iterator[Event]:
        kind = p.get("type")
        if kind in ("function_call", "custom_tool_call"):
            cid = p.get("call_id")
            if not isinstance(cid, str):
                return
            name = str(p.get("name") or "unknown")
            raw_input = p.get("arguments") if kind == "function_call" else p.get("input")
            self._call_ids.add(cid)
            action_kind, target, fp = normalise_tool_call(name, raw_input, self.cwd)
            yield ToolUseEvent(cid, ts, name, action_kind, target, fp)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            cid = p.get("call_id")
            if isinstance(cid, str):
                yield ToolResultEvent(cid, ts, _error_from_output(p.get("output")))

    def _item(self, p: dict[str, Any], ts: str | None) -> Iterator[Event]:
        item = p.get("item")
        if not isinstance(item, dict):
            return
        kind, iid = item.get("type"), item.get("id")
        if not isinstance(iid, str) or iid in self._call_ids:
            return
        if kind == "McpToolCall":
            tool = f"mcp:{item.get('server') or '?'}/{item.get('tool') or '?'}"[:96]
            yield ToolUseEvent(iid, ts, tool, "mcp", None, {"tool": tool, "arguments": item.get("arguments")})
            failed = item.get("status") == "failed" or bool(item.get("error"))
            yield ToolResultEvent(iid, ts, failed if item.get("status") in ("completed", "failed") else None)
        elif kind == "WebSearch":
            yield ToolUseEvent(iid, ts, "web_search", "web", None, {"tool": "web_search", "query": item.get("query")})
            yield ToolResultEvent(iid, ts, None)
