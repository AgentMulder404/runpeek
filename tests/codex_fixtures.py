"""Synthetic Codex rollout builder plus the sanitised real-record fixtures.

Shapes follow the structural inspection of real rollouts written by Codex CLI
0.130–0.153 (record types, key names, cumulative token_count semantics,
subagent markers). Every content field carries a SENTINEL so privacy tests can
assert nothing content-like is ever persisted. The files under
``fixtures/codex`` are real records passed through ``fixtures/codex/sanitize.py``.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
SENTINELS = ("SECRET_COMMAND_PAYLOAD", "SECRET_TOOL_OUTPUT", "SECRET_PATCH_CONTENT", "SECRET_PROMPT_TEXT",
             "SECRET_ASSISTANT_TEXT")
CLI_VERSION = "0.153.1"
MODEL = "gpt-5.5"
CWD = "/work/codex-project"


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid7ish() -> str:
    return str(uuid.uuid4())


class Rollout:
    """One rollout file under ``<home>/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``."""

    def __init__(self, home: Path, cwd: str = CWD, *, version: str = CLI_VERSION, model: str = MODEL,
                 start: datetime | None = None, thread_id: str | None = None, parent: Rollout | None = None,
                 history_start_ordinal: int | None = None, branch: str | None = "main") -> None:
        self.home = home
        self.cwd = cwd
        self.version = version
        self.model = model
        self.t = start or datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        self.thread_id = thread_id or _uuid7ish()
        self.parent = parent
        self.ordinal = 0
        self.turn_id: str | None = None
        self.total = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                      "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0}
        self.last = dict(self.total)
        day = self.t.astimezone(timezone.utc)
        d = home / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        d.mkdir(parents=True, exist_ok=True)
        stamp = f"{day:%Y-%m-%dT%H-%M-%S}"
        self.path = d / f"rollout-{stamp}-{self.thread_id}.jsonl"
        self.path.touch()
        meta: dict[str, Any] = {
            "session_id": parent.thread_id if parent else self.thread_id, "id": self.thread_id,
            "timestamp": _iso(self.t), "cwd": cwd, "originator": "Codex Desktop", "cli_version": version,
            "source": "vscode", "thread_source": "subagent" if parent else "user", "model_provider": "openai",
            "base_instructions": {"text": "SECRET_PROMPT_TEXT"}, "history_mode": "paginated",
        }
        if branch:
            meta["git"] = {"branch": branch, "commit_hash": "0" * 40}
        if parent:
            meta.update(parent_thread_id=parent.thread_id, forked_from_id=parent.thread_id,
                        subagent_history_start_ordinal=history_start_ordinal or 0, agent_nickname="agent",
                        agent_path="/root/agent", multi_agent_version="v2")
        self.append("session_meta", meta)

    # ---- raw

    def advance(self, seconds: float) -> datetime:
        self.t = self.t + timedelta(seconds=seconds)
        return self.t

    def append_raw(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def append(self, typ: str, payload: dict[str, Any], *, ordinal: int | None = None) -> int:
        o = self.ordinal if ordinal is None else ordinal
        self.append_raw(json.dumps({"timestamp": _iso(self.t), "ordinal": o, "type": typ, "payload": payload}) + "\n")
        if ordinal is None:
            self.ordinal += 1
        return o

    # ---- entries

    def user_turn(self, seconds: float = 1.0, *, model: str | None = None) -> str:
        self.advance(seconds)
        self.turn_id = _uuid7ish()
        self.append("event_msg", {"type": "task_started", "turn_id": self.turn_id,
                                  "started_at": int(self.t.timestamp()), "model_context_window": 258400})
        self.append("turn_context", {"turn_id": self.turn_id, "cwd": self.cwd, "model": model or self.model,
                                     "approval_policy": "never"})
        self.append("response_item", {"type": "message", "id": f"msg_{_uuid7ish()}", "role": "user",
                                      "content": [{"type": "input_text", "text": "SECRET_PROMPT_TEXT"}]})
        return self.turn_id

    def message(self, seconds: float = 0.5) -> None:
        self.advance(seconds)
        self.append("response_item", {"type": "message", "id": f"msg_{_uuid7ish()}", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "SECRET_ASSISTANT_TEXT"}]})

    def usage(self, inp: int, cached: int, out: int, reasoning: int = 0, *, seconds: float = 1.0,
              response_id: str | None = None, repeat_stale: bool = False) -> int:
        """One provider response. With ``repeat_stale`` the totals are not advanced and the
        previous ``last_token_usage`` is written again (the pattern seen after ``turn_aborted``)."""
        self.advance(seconds)
        if not repeat_stale:
            self.last = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                         "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}
            for k, v in self.last.items():
                self.total[k] += v
            if response_id:
                self.append("token_usage_record", {"thread_id": self.thread_id, "turn_id": self.turn_id,
                                                   "session_id": self.thread_id, "response_id": response_id,
                                                   "usage": dict(self.last)})
        return self.append("event_msg", {"type": "token_count", "info": {
            "total_token_usage": dict(self.total), "last_token_usage": dict(self.last),
            "model_context_window": 258400}, "rate_limits": {"limit_id": "codex"}})

    def exec(self, command: str, *, exit_code: int | None = 0, seconds: float = 1.0, as_function: bool = False) -> str:
        self.advance(seconds)
        cid = f"call_{uuid.uuid4().hex[:22]}"
        if as_function:
            self.append("response_item", {"type": "function_call", "id": f"fc_{_uuid7ish()}", "name": "exec_command",
                                          "arguments": json.dumps({"cmd": f"{command} SECRET_COMMAND_PAYLOAD",
                                                                   "workdir": self.cwd}), "call_id": cid})
            out: Any = json.dumps({"output": "SECRET_TOOL_OUTPUT", "metadata": {"exit_code": exit_code}})
            self.advance(0.2)
            self.append("response_item", {"type": "function_call_output", "id": f"fco_{_uuid7ish()}",
                                          "call_id": cid, "output": out})
        else:
            self.append("response_item", {"type": "custom_tool_call", "id": f"ctc_{_uuid7ish()}",
                                          "status": "completed", "call_id": cid, "name": "exec",
                                          "input": f"{command} SECRET_COMMAND_PAYLOAD"})
            self.advance(0.2)
            text = ("SECRET_TOOL_OUTPUT" if exit_code is None else f"Exit code: {exit_code}\nSECRET_TOOL_OUTPUT")
            self.append("response_item", {"type": "custom_tool_call_output", "id": f"ctco_{_uuid7ish()}",
                                          "call_id": cid, "output": text})
        return cid

    def patch(self, rel: str, *, seconds: float = 1.0) -> str:
        self.advance(seconds)
        cid = f"call_{uuid.uuid4().hex[:22]}"
        self.append("response_item", {"type": "custom_tool_call", "id": f"ctc_{_uuid7ish()}", "status": "completed",
                                      "call_id": cid, "name": "apply_patch",
                                      "input": f"*** Begin Patch\n*** Update File: {rel}\nSECRET_PATCH_CONTENT\n"
                                               "*** End Patch"})
        self.advance(0.2)
        self.append("response_item", {"type": "custom_tool_call_output", "id": f"ctco_{_uuid7ish()}", "call_id": cid,
                                      "output": "Success. Updated the following files:\nM " + rel})
        return cid

    def mcp_item(self, server: str, tool: str, *, failed: bool = False, seconds: float = 1.0) -> str:
        self.advance(seconds)
        iid = f"exec-{_uuid7ish()}"
        self.append("event_msg", {"type": "item_completed", "turn_id": self.turn_id, "item": {
            "type": "McpToolCall", "id": iid, "server": server, "tool": tool,
            "status": "failed" if failed else "completed",
            "error": {"message": "SECRET_TOOL_OUTPUT"} if failed else None,
            "arguments": {"q": "SECRET_COMMAND_PAYLOAD"}, "result": "SECRET_TOOL_OUTPUT"}})
        return iid

    def spawn_agent(self, child_thread_id: str, *, seconds: float = 1.0) -> str:
        self.advance(seconds)
        cid = f"call_{uuid.uuid4().hex[:22]}"
        self.append("response_item", {"type": "function_call", "id": f"fc_{_uuid7ish()}", "name": "spawn_agent",
                                      "arguments": json.dumps({"task_name": "x", "message": "SECRET_PROMPT_TEXT"}),
                                      "call_id": cid})
        self.append("event_msg", {"type": "item_completed", "turn_id": self.turn_id, "item": {
            "type": "SubAgentActivity", "id": cid, "kind": "started", "agent_thread_id": child_thread_id,
            "agent_path": "/root/agent"}})
        self.append("response_item", {"type": "function_call_output", "id": f"fco_{_uuid7ish()}", "call_id": cid,
                                      "output": json.dumps({"accepted": True})})
        return cid

    def complete(self, seconds: float = 0.5) -> None:
        self.advance(seconds)
        self.append("event_msg", {"type": "task_complete", "turn_id": self.turn_id,
                                  "last_agent_message": "SECRET_ASSISTANT_TEXT"})

    def abort(self, seconds: float = 0.5, *, duration_ms: int = 1400) -> None:
        self.advance(seconds)
        self.append("event_msg", {"type": "turn_aborted", "turn_id": self.turn_id, "reason": "interrupted",
                                  "duration_ms": duration_ms})

    def compact(self) -> None:
        self.append("compacted", {"message": "", "replacement_history": [], "window_number": 1})


def install_real_fixture(home: Path, name: str) -> Path:
    """Copy a sanitised real rollout into ``home`` at the path Codex would use."""
    src = FIXTURES / name
    stamp = name[len("rollout-"):len("rollout-") + 10]  # YYYY-MM-DD
    y, m, d = stamp.split("-")
    dst = home / "sessions" / y / m / d / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    return dst


REAL_STALE = "rollout-2026-07-10T11-24-48-019f4d46-6abe-74c0-84fb-3e50834bfc15.jsonl"
REAL_PARENT = "rollout-2026-09-04T16-01-07-01a06ea7-8600-7a80-8612-a3eb93a25cea.jsonl"
REAL_SUBAGENT = "rollout-2026-09-06T14-30-10-01a078a0-f982-7503-b280-d912d16d5d41.jsonl"
