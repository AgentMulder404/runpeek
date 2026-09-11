"""Setup, collection, ledger and optional self-hosted sync commands."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import ssl
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import collection, hub, ledger, recovery, sync
from .agents import work
from .agents.ingest import ADAPTERS, Ingestor
from .store import apply_schema, open_connection


def connect_db(args: argparse.Namespace) -> sqlite3.Connection:
    from .cli import resolve_db
    path, _ = resolve_db(args.db)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = open_connection(path)
    apply_schema(conn)
    ledger.setup(conn)
    return conn


def consent(message: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("Interactive confirmation required; pass --yes after reviewing the collection scope")
    return input(message + " [y/N] ").strip().lower() in ("y", "yes")


def run(args: argparse.Namespace) -> int:
    conn = None
    try:
        conn = connect_db(args)
        command = args.unified
        if command == "init":
            project = str(Path(args.project or Path.cwd()).resolve())
            files = [f for adapter in ADAPTERS.values() for f in adapter.discover(project)]
            print(f"Found {len(files)} local session files for {project}.")
            for source, adapter in ADAPTERS.items():
                print(f"  {adapter.LABEL}: {sum(f.source == source for f in files)}")
            print("Local collection only. Stores usage, model IDs, paths, tool labels and hashed action fingerprints; "
                  "no prompts, responses or tool argument text. Nothing is uploaded.")
            if not consent("Collect this project's records?", args.yes):
                return 0
            sync.setting(conn, "project", project)
            sync.setting(conn, "collection_paused", "false")
            ing = Ingestor(conn)
            for tf in files:
                ing.ingest(tf)
            name = args.name
            if name is None and sys.stdin.isatty():
                name = input("Name your current work item (Enter to skip): ").strip() or None
            if name:
                wid = work.create(conn, name, "task", repository=project)
                roots = [f for f in files if not f.is_subagent]
                if roots:
                    latest = max(roots, key=lambda f: f.path.stat().st_mtime)
                    print(f"Latest session: {latest.session_id}. Earlier sessions remain unassigned.")
                    if consent("Attach the latest session and its subagents to this work?", args.yes):
                        work.assign(conn, wid, [latest.session_id])
                sync.setting(conn, "active_work", wid)
                print(work.render_report(work.build_report(conn, wid)))
            print("Ready. Run `runpeek collect` for an updated report. Custom agents: runpeek.tracking.Tracker.")
            print("Other platforms are not automatically connected. Optional device sync: runpeek connect URL.")
        elif command == "join":
            if not ledger.IDENTIFIER.fullmatch(args.work_item):
                raise ValueError("Invalid shared work ID")
            if work.get(conn, args.work_item) is None:
                created = work.create(conn, args.name or args.work_item, "task")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("UPDATE work_items SET work_item_id=? WHERE work_item_id=?",
                                 (args.work_item, created))
                    conn.execute("UPDATE work_item_events SET work_item_id=? WHERE work_item_id=?",
                                 (args.work_item, created))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            sync.setting(conn, "active_work", args.work_item)
            print("Joined shared work " + args.work_item + "; attach existing sessions with work assign.")
        elif command == "activate":
            ref = work.resolve(conn, args.work_item)
            if not isinstance(ref, str):
                raise ValueError("Work item not found or ambiguous")
            sync.setting(conn, "active_work", ref)
            print(f"Active work: {ref}. Newly discovered root sessions will be attached during collect.")
        elif command == "collect":
            if sync.setting(conn, "collection_paused") == "true":
                print("Collection paused. Use `runpeek resume`.")
                return 0
            project = sync.setting(conn, "project") or str(Path.cwd())
            known = {r[0] for r in conn.execute("SELECT session_id FROM agent_sessions")}
            active = sync.setting(conn, "active_work")
            ing = Ingestor(conn)
            for adapter in ADAPTERS.values():
                for tf in adapter.discover(project):
                    ing.ingest(tf)
                    if active and tf.session_id not in known and not tf.is_subagent:
                        work.assign(conn, active, [tf.session_id])
            print(json.dumps(collection.collect(conn), indent=2))
            print(ledger.render(ledger.report(conn, "local", active)))
        elif command in ("pause", "resume"):
            value = "true" if command == "pause" else "false"
            sync.setting(conn, "collection_paused", value)
            sync.setting(conn, "paused", value)
            print("Collection and sync " + ("paused" if command == "pause" else "resumed"))
        elif command == "status":
            print(json.dumps({"project": sync.setting(conn, "project"),
                "active_work": sync.setting(conn, "active_work"),
                "collection_paused": sync.setting(conn, "collection_paused") == "true",
                "sync_endpoint": sync.setting(conn, "endpoint"), "device_id": sync.setting(conn, "device_id"),
                "credential_location": "OS credential store (when connected)",
                "tracking_model_tokens": 0, "report": ledger.report(conn, "local")}, indent=2))
        elif command == "ledger":
            action = args.action
            if action == "report":
                print(ledger.render(ledger.report(conn, "local", args.work_item)))
            elif action == "assign":
                if not args.charge_key or not args.work_item:
                    raise ValueError("Choose --charge-key and --work-item")
                ledger.assign(conn, "local", args.charge_key, args.work_item)
                print("Local attribution corrected. Run sync to update charges this device has observed on the hub.")
            elif action == "import":
                # Strict JSONL accounting schema, not general chat export parsing.
                count = 0
                if not args.file:
                    raise ValueError("Choose --file containing accounting JSONL")
                with open(args.file, "rb") as stream:
                    while raw := stream.readline(16385):
                        if len(raw) > 16384:
                            raise ValueError("Import record exceeds 16KB")
                        count += ledger.ingest(conn, "local", [json.loads(raw)])
                print(f"Imported {count} observations")
            elif action == "export":
                if not args.file:
                    raise ValueError("Choose --file for the private metadata export")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                with os.fdopen(os.open(args.file, flags, 0o600), "w") as output:
                    for event in ledger.export(conn, "local"):
                        output.write(ledger.canonical(event) + "\n")
                print("Export written with owner-only permissions")
            elif action == "delete":
                if consent("Delete ledger observations? Source transcripts and assignments are retained.", args.yes):
                    print(f"Deleted {ledger.delete(conn, 'local', args.before)} charges; replay tombstones retained.")
                    print("Remote data is separate: ask the workspace administrator to delete it on the hub.")
        elif command == "repair":
            from .cli import resolve_db
            path, _ = resolve_db(args.db)
            conn.close()
            conn = None
            print(f"Rebuilt agent accounting; backup: {recovery.repair(path)}")
        elif command == "connect":
            endpoint = sync.endpoint_url(args.endpoint)
            print("Sync sends assigned work IDs, opaque session IDs, model/provider/request IDs, timestamps, tokens "
                  "and costs. It excludes prompts, responses, paths and work names.")
            if not consent(f"Authorize this device for {endpoint}?", args.yes):
                return 0
            sync.credential(endpoint)  # fail before pairing if secure storage is unavailable
            pair = sync.request(endpoint, "/v1/pair/start", {})
            print(f"Device code: {pair['user_code']}. Ask your hub administrator to approve it.", flush=True)
            webbrowser.open(endpoint + "/pair")
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                time.sleep(5)
                result = sync.request(endpoint, "/v1/pair/poll", {"device_secret": pair["device_secret"]})
                if result.get("pending"):
                    continue
                sync.credential(endpoint, result["token"])
                sync.setting(conn, "endpoint", endpoint)
                sync.setting(conn, "device_id", result["device_id"])
                sync.setting(conn, "paused", "false")
                conn.execute("DELETE FROM ledger_outbox WHERE endpoint=?", (endpoint,))
                conn.execute("DELETE FROM ledger_assignment_receipts WHERE endpoint=?", (endpoint,))
                print("Device connected. Run `runpeek sync` when ready to upload; no data uploaded yet.")
                break
            else:
                raise ValueError("Device approval timed out; start connect again")
        elif command == "sync":
            print(json.dumps(sync.push(conn, force=args.force), indent=2))
        elif command == "disconnect":
            endpoint = sync.setting(conn, "endpoint") or ""
            if endpoint:
                token = sync.credential(endpoint)
                if token:
                    sync.request(endpoint, "/v1/disconnect", {}, token)
                sync.credential(endpoint, remove=True)
                conn.execute("DELETE FROM ledger_settings WHERE key IN ('endpoint','device_id')")
            print("Device revoked and local credential removed. Existing remote observations are retained.")
        elif command == "hub":
            hub.setup(conn)
            if args.action == "approve":
                hub.approve(conn, args.code, args.workspace)
                print("Device pairing approved for workspace " + args.workspace)
            elif args.action == "assign":
                if not args.charge_key or not args.work_item:
                    raise ValueError("Choose --charge-key and --work-item")
                ledger.assign(conn, args.workspace, args.charge_key, args.work_item)
                print("Workspace attribution corrected")
            elif args.action == "devices":
                rows = conn.execute("SELECT id,role,expires,revoked FROM hub_devices WHERE workspace=?",
                                    (args.workspace,)).fetchall()
                print(json.dumps([dict(r) for r in rows], indent=2))
            elif args.action == "revoke":
                hub.revoke(conn, args.workspace, args.device)
                print("Device revoked")
            elif args.action == "report":
                print(ledger.render(ledger.report(conn, args.workspace)))
            elif args.action == "export":
                if not args.file:
                    raise ValueError("Choose --file for the private workspace export")
                with os.fdopen(os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
                    for event in ledger.export(conn, args.workspace):
                        output.write(ledger.canonical(event) + "\n")
            elif args.action == "delete":
                if consent("Delete this workspace's observations?", args.yes):
                    print(ledger.delete(conn, args.workspace, args.before))
            elif args.action == "serve":
                from wsgiref.simple_server import WSGIRequestHandler, make_server

                from .cli import resolve_db
                if args.host not in ("127.0.0.1", "::1", "localhost") and not (args.cert and args.key):
                    raise ValueError("Remote listening requires --cert and --key; default is loopback only")
                path, _ = resolve_db(args.db)

                class QuietHandler(WSGIRequestHandler):
                    def log_message(self, format: str, *values: Any) -> None:
                        pass  # Never log URLs, device codes, tokens, or bodies.

                    def handle(self) -> None:
                        self.connection.settimeout(10)
                        super().handle()

                with make_server(args.host, args.port, hub.Application(path), handler_class=QuietHandler) as server:
                    if args.cert and args.key:
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        context.minimum_version = ssl.TLSVersion.TLSv1_2
                        context.load_cert_chain(args.cert, args.key)
                        server.socket = context.wrap_socket(server.socket, server_side=True)
                    print(f"Receiver listening on {args.host}:{args.port}. Private pilot server; see docs/UNIFIED.md.",
                          flush=True)
                    server.serve_forever()
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Error text from network/keychain backends can contain credentials.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print("runpeek: " + message, file=sys.stderr)
        return 2
    finally:
        if conn:
            conn.close()


def register(sub: Any) -> None:
    def command(name: str, help: str) -> Any:
        parser = sub.add_parser(name, help=help)
        parser.add_argument("--db")
        parser.set_defaults(fn=run, unified=name)
        return parser

    p = command("init", "detect local agents and get your first report without uploads")
    p.add_argument("--project")
    p.add_argument("--name")
    p.add_argument("--yes", action="store_true")
    p = command("join", "use the same work ID on another device")
    p.add_argument("work_item")
    p.add_argument("--name")
    p = command("activate", "remember the work item for newly discovered sessions")
    p.add_argument("work_item")
    for name in ("collect", "pause", "resume", "status", "repair", "disconnect"):
        command(name, name + " local accounting")
    p = command("connect", "pair a device with a self-hosted receiver")
    p.add_argument("endpoint")
    p.add_argument("--yes", action="store_true")
    p = command("sync", "upload approved accounting metadata")
    p.add_argument("--force", action="store_true")
    p = command("ledger", "report/import/export/delete metadata-only observations")
    p.add_argument("action", choices=("report", "import", "export", "delete", "assign"))
    p.add_argument("--work-item")
    p.add_argument("--charge-key")
    p.add_argument("--file")
    p.add_argument("--before")
    p.add_argument("--yes", action="store_true")
    p = command("hub", "operate a private shared accounting receiver")
    p.add_argument("action", choices=("serve", "approve", "devices", "revoke", "report", "export", "delete", "assign"))
    p.add_argument("code", nargs="?")
    p.add_argument("--workspace", default="default")
    p.add_argument("--device")
    p.add_argument("--charge-key")
    p.add_argument("--work-item")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--file")
    p.add_argument("--before")
    p.add_argument("--yes", action="store_true")
