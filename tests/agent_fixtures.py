"""Sanitised Claude Code transcript fixtures.

Shapes follow the structural inspection of real 2.1.x transcripts (entry
types, key names, usage layout). Every content field carries a SENTINEL so
privacy tests can assert nothing content-like is ever persisted.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SENTINELS = ("SECRET_PROMPT_TEXT", "SECRET_TOOL_OUTPUT", "SECRET_ASSISTANT_TEXT", "SECRET_COMMAND_PAYLOAD",
             "SECRET_FILE_CONTENT", "SECRET_OLD_STRING", "SECRET_NEW_STRING")

WRITER_VERSION = "2.1.250"
MODEL = "claude-opus-5"


def encode(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", project)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Transcript:
    def __init__(self, home: Path, project: str, session_id: str | None = None, *, version: str = WRITER_VERSION,
                 start: datetime | None = None, subagent_of: str | None = None) -> None:
        self.home = home
        self.project = project
        self.session_id = session_id or str(uuid.uuid4())
        self.version = version
        self.t = start or datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        pdir = home / "projects" / encode(project)
        if subagent_of:
            pdir = pdir / subagent_of / "subagents"
        pdir.mkdir(parents=True, exist_ok=True)
        self.path = pdir / f"{self.session_id}.jsonl"
        self.path.touch()
        self.last_uuid: str | None = None
        self.prompt_id: str | None = None

    # ---- clock

    def advance(self, seconds: float) -> datetime:
        self.t = self.t + timedelta(seconds=seconds)
        return self.t

    # ---- raw

    def _base(self, typ: str) -> dict[str, Any]:
        u = str(uuid.uuid4())
        e = {
            "type": typ, "uuid": u, "parentUuid": self.last_uuid, "sessionId": self.session_id,
            "cwd": self.project, "version": self.version, "timestamp": _iso(self.t), "isSidechain": False,
            "userType": "external", "gitBranch": "main",
        }
        self.last_uuid = u
        return e

    def append_raw(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def append(self, entry: dict[str, Any]) -> None:
        self.append_raw(json.dumps(entry) + "\n")

    # ---- entries

    def user_prompt(self, seconds: float = 1.0) -> str:
        self.advance(seconds)
        self.prompt_id = str(uuid.uuid4())
        e = self._base("user")
        e.update(promptId=self.prompt_id, message={"role": "user", "content": "SECRET_PROMPT_TEXT"})
        self.append(e)
        return self.prompt_id

    def assistant(self, blocks: list[dict[str, Any]], *, usage: dict[str, Any] | None = None, model: str = MODEL,
                  msg_id: str | None = None, seconds: float = 1.0, repeat_entries: int = 1) -> str:
        """One API response. `repeat_entries` > 1 mimics streaming: several
        transcript entries sharing message.id and identical usage."""
        self.advance(seconds)
        msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        req_id = f"req_{uuid.uuid4().hex[:24]}"
        for i in range(repeat_entries):
            e = self._base("assistant")
            e.update(requestId=req_id, effort="high")
            msg: dict[str, Any] = {"id": msg_id, "type": "message", "role": "assistant", "model": model,
                                   "content": [blocks[i]] if repeat_entries > 1 and i < len(blocks) else blocks,
                                   "stop_reason": "tool_use"}
            if usage is not None:
                msg["usage"] = usage
            e["message"] = msg
            self.append(e)
        return msg_id

    def tool_use(self, name: str, inp: dict[str, Any], *, usage: dict[str, Any] | None = None, seconds: float = 1.0,
                 repeat_entries: int = 1) -> str:
        tid = f"toolu_{uuid.uuid4().hex[:20]}"
        blocks: list[dict[str, Any]] = [{"type": "text", "text": "SECRET_ASSISTANT_TEXT"},
                                        {"type": "tool_use", "id": tid, "name": name, "input": inp}]
        self.assistant(blocks, usage=usage or usage_block(), seconds=seconds, repeat_entries=repeat_entries)
        return tid

    def tool_result(self, tool_use_id: str, *, is_error: bool | None = False, seconds: float = 0.5) -> None:
        self.advance(seconds)
        e = self._base("user")
        block: dict[str, Any] = {"tool_use_id": tool_use_id, "type": "tool_result", "content": "SECRET_TOOL_OUTPUT"}
        if is_error is not None:
            block["is_error"] = is_error
        e.update(message={"role": "user", "content": [block]}, toolUseResult={"stdout": "SECRET_TOOL_OUTPUT"})
        self.append(e)

    def text(self, *, usage: dict[str, Any] | None = None, seconds: float = 1.0) -> str:
        return self.assistant([{"type": "text", "text": "SECRET_ASSISTANT_TEXT"}], usage=usage or usage_block(),
                              seconds=seconds)

    def turn_duration(self, ms: int, seconds: float = 0.1) -> None:
        self.advance(seconds)
        e = self._base("system")
        e.update(subtype="turn_duration", durationMs=ms, content="", level="info", isMeta=True)
        self.append(e)

    # ---- convenience

    def bash(self, command: str, *, is_error: bool = False, seconds: float = 1.0) -> str:
        tid = self.tool_use("Bash", {"command": command, "description": "SECRET_COMMAND_PAYLOAD"}, seconds=seconds)
        self.tool_result(tid, is_error=is_error)
        return tid

    def read(self, rel: str, *, offset: int | None = None, seconds: float = 1.0) -> str:
        inp: dict[str, Any] = {"file_path": f"{self.project}/{rel}"}
        if offset is not None:
            inp["offset"] = offset
        tid = self.tool_use("Read", inp, seconds=seconds)
        self.tool_result(tid, is_error=False)
        return tid

    def edit(self, rel: str, *, seconds: float = 1.0) -> str:
        tid = self.tool_use("Edit", {"file_path": f"{self.project}/{rel}", "old_string": "SECRET_OLD_STRING",
                                     "new_string": "SECRET_NEW_STRING"}, seconds=seconds)
        self.tool_result(tid, is_error=False)
        return tid


def usage_block(inp: int = 1000, out: int = 200, cw5: int = 300, cw1: int = 0, cr: int = 5000) -> dict[str, Any]:
    return {
        "input_tokens": inp, "cache_creation_input_tokens": cw5 + cw1, "cache_read_input_tokens": cr,
        "output_tokens": out, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {"ephemeral_5m_input_tokens": cw5, "ephemeral_1h_input_tokens": cw1},
        "inference_geo": "global", "iterations": [], "speed": "standard",
    }
