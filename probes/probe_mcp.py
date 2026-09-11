#!/usr/bin/env python3
"""Dependency-free stdio MCP server for one probe: tool `env_probe` returns the process
environment keys that look like session/thread identity, plus the tool call's _meta."""
import json, os, sys
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
KEYS = [k for k in os.environ if any(s in k.upper() for s in ("CLAUDE", "CODEX", "SESSION", "THREAD", "MCP", "OTEL", "CONVERSATION"))]
snapshot = {k: (os.environ[k][:12] + "…" if len(os.environ[k]) > 12 else os.environ[k]) for k in KEYS}
with open(os.path.expanduser("~/runpeek-probe-env.json"), "w") as fh:
    json.dump({"argv": sys.argv, "cwd": os.getcwd(), "env": snapshot, "ppid": os.getppid()}, fh)
for line in sys.stdin:
    try: msg = json.loads(line)
    except ValueError: continue
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "runpeek-probe", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "env_probe", "description": "Return identity hints visible to this server.",
              "inputSchema": {"type": "object", "properties": {}}}]}})
    elif method == "tools/call":
        info = {"env": snapshot, "cwd": os.getcwd(), "_meta": params.get("_meta"), "argv": sys.argv[1:]}
        with open(os.path.expanduser("~/runpeek-probe-call.json"), "w") as fh: json.dump(info, fh)
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(info)}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not supported"}})
