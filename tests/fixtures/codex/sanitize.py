"""Turn a real Codex rollout into a structure-only fixture.

Keeps: record types, ordinals, timestamps, ids, turn ids, models, token
counts, tool names, exit codes, subagent/fork markers, branch name.
Drops or replaces with sentinels: prompts, messages, reasoning, tool inputs
and outputs, instructions, world state, rate limits, commit hashes,
repository urls, real paths (the working directory is mapped to /work/codex-project).

    python tests/fixtures/codex/sanitize.py <rollout.jsonl> <out.jsonl> [max_ordinal]
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

CWD = "/work/codex-project"
SENTINEL_CMD = "SECRET_COMMAND_PAYLOAD"
SENTINEL_OUT = "SECRET_TOOL_OUTPUT"
SENTINEL_PATCH = "SECRET_PATCH_CONTENT"
_EXIT_RE = re.compile(r"(?i)\bexit(?:ed)?(?: with)? code[:\s]+(-?\d+)")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+)$", re.M)


def _program(cmd: str) -> str:
    for t in cmd.strip().split():
        if "=" in t and not t.startswith("-"):
            continue
        if t in ("sudo", "env", "nohup", "time", "exec", "bash", "sh", "zsh", "-lc", "-c"):
            continue
        return os.path.basename(t)[:64]
    return "cmd"


def _map_path(p: Any, real_cwd: str | None) -> Any:
    if isinstance(p, str) and real_cwd and p.startswith(real_cwd):
        return CWD + p[len(real_cwd):]
    return p


def sanitize(lines: list[str], max_ordinal: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out: list[dict[str, Any]] = []
    real_cwd: str | None = None
    stats: dict[str, Any] = {"token_count": 0, "models": set(), "tool_calls": 0}
    for line in lines:
        d = json.loads(line)
        o = d.get("ordinal")
        if max_ordinal is not None and isinstance(o, int) and o > max_ordinal:
            break
        t, p = d.get("type"), d.get("payload") or {}
        keep: dict[str, Any] | None = None
        if t == "session_meta":
            real_cwd = real_cwd or (p.get("cwd") if isinstance(p.get("cwd"), str) else None)
            keep = {k: p[k] for k in ("session_id", "id", "timestamp", "originator", "cli_version", "source",
                                      "thread_source", "model_provider", "parent_thread_id", "forked_from_id",
                                      "subagent_history_start_ordinal", "history_mode", "multi_agent_version")
                    if k in p}
            keep["cwd"] = CWD
            if isinstance(p.get("git"), dict) and p["git"].get("branch"):
                keep["git"] = {"branch": p["git"]["branch"]}
            if "agent_nickname" in p:
                keep["agent_nickname"] = "agent"
            if "agent_path" in p:
                keep["agent_path"] = p["agent_path"]
        elif t == "turn_context":
            keep = {k: p[k] for k in ("turn_id", "model") if k in p}
            keep["cwd"] = CWD
            stats["models"].add(p.get("model"))
        elif t == "event_msg":
            k = p.get("type")
            if k == "task_started":
                keep = {kk: p[kk] for kk in ("type", "turn_id", "started_at", "model_context_window") if kk in p}
            elif k == "task_complete":
                keep = {"type": k, "turn_id": p.get("turn_id")}
            elif k == "turn_aborted":
                keep = {kk: p[kk] for kk in ("type", "turn_id", "reason", "started_at", "completed_at", "duration_ms")
                        if kk in p}
            elif k == "token_count":
                keep = {"type": k, "info": p.get("info")}
                if p.get("info"):
                    stats["token_count"] += 1
            elif k == "thread_settings_applied":
                ts = p.get("thread_settings") or {}
                keep = {"type": k, "thread_settings": {"model": ts.get("model")}}
            elif k == "item_completed":
                it = p.get("item") or {}
                kind = it.get("type")
                if kind == "McpToolCall":
                    keep = {"type": k, "turn_id": p.get("turn_id"),
                            "item": {"type": kind, "id": it.get("id"), "status": it.get("status"),
                                     "error": bool(it.get("error")) or None, "server": it.get("server"),
                                     "tool": it.get("tool")}}
                elif kind == "WebSearch":
                    keep = {"type": k, "turn_id": p.get("turn_id"), "item": {"type": kind, "id": it.get("id")}}
                elif kind == "SubAgentActivity":
                    keep = {"type": k, "turn_id": p.get("turn_id"), "item": dict(it)}
        elif t == "response_item":
            k = p.get("type")
            if k in ("function_call", "custom_tool_call"):
                stats["tool_calls"] += 1
                name = p.get("name")
                keep = {"type": k, "id": p.get("id"), "call_id": p.get("call_id"), "name": name}
                raw = p.get("arguments") if k == "function_call" else p.get("input")
                if name in ("exec", "shell") and isinstance(raw, str):
                    keep["input"] = f"{_program(raw)} {SENTINEL_CMD}"
                elif name == "exec_command":
                    cmd = None
                    try:
                        cmd = json.loads(raw).get("cmd") if isinstance(raw, str) else None
                    except ValueError:
                        pass
                    keep["arguments"] = json.dumps({"cmd": f"{_program(cmd) if isinstance(cmd, str) else 'cmd'} "
                                                           f"{SENTINEL_CMD}"})
                elif name == "apply_patch" and isinstance(raw, str):
                    files = [f"*** {op} File: {_map_path(path, real_cwd)}" for op, path in _PATCH_FILE_RE.findall(raw)]
                    keep["input"] = "*** Begin Patch\n" + "\n".join(files[:3]) + f"\n{SENTINEL_PATCH}\n*** End Patch"
                elif name == "view_image":
                    keep["arguments"] = json.dumps({"path": CWD + "/SECRET_IMAGE.png"})
                elif k == "function_call":
                    keep["arguments"] = json.dumps({"payload": SENTINEL_CMD})
                else:
                    keep["input"] = SENTINEL_CMD
            elif k in ("function_call_output", "custom_tool_call_output"):
                keep = {"type": k, "id": p.get("id"), "call_id": p.get("call_id")}
                o_raw = p.get("output")
                exit_code: int | None = None
                if isinstance(o_raw, str):
                    s = o_raw.lstrip()
                    if s.startswith("{"):
                        try:
                            meta = json.loads(s).get("metadata") or {}
                            exit_code = int(meta["exit_code"]) if "exit_code" in meta else None
                        except (ValueError, TypeError, AttributeError):
                            exit_code = None
                        keep["output"] = json.dumps({"output": SENTINEL_OUT, "metadata": {"exit_code": exit_code}}
                                                    if exit_code is not None else {"output": SENTINEL_OUT})
                    else:
                        m = _EXIT_RE.search(s[:200])
                        keep["output"] = (f"Exit code: {m.group(1)}\n{SENTINEL_OUT}" if m else SENTINEL_OUT)
                else:
                    keep["output"] = SENTINEL_OUT
        elif t == "token_usage_record":
            keep = {k: p[k] for k in ("thread_id", "turn_id", "session_id", "root_turn_id", "response_id", "usage")
                    if k in p}
        if keep is None:
            continue
        out.append({"timestamp": d.get("timestamp"), "ordinal": o, "type": t, "payload": keep})
    stats["models"] = sorted(m for m in stats["models"] if m)
    return out, stats


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    max_ord = int(sys.argv[3]) if len(sys.argv) > 3 else None
    with open(src, encoding="utf-8") as fh:
        lines = fh.readlines()
    recs, stats = sanitize(lines, max_ord)
    with open(dst, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    print(json.dumps({"records": len(recs), **stats}))


if __name__ == "__main__":
    main()
