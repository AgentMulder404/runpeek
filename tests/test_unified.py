"""Independent accounting, tenant isolation, privacy and real HTTP acceptance tests."""
from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path
from wsgiref.simple_server import make_server

import pytest

from codex_fixtures import CWD, Rollout
from runpeek import collection, hub, ledger, recovery, sync
from runpeek.agents import codex, work
from runpeek.agents.ingest import Ingestor
from runpeek.agents.report import render_sessions
from runpeek.store import apply_schema, open_connection
from runpeek.tracking import Tracker, task


def event(**changes: object) -> dict:
    return dict(schema_version=1, event_id="event-1", source="sdk", work_item_id="feature-1", agent="custom",
                session_id="session-1", provider="openai", account_scope="default", request_id="request-1",
                at="2026-09-11T00:00:00+00:00", model="gpt-5", basis="estimated", amount_nanos=1000,
                input_tokens=10, output_tokens=2, **changes) if not changes else {**event(), **changes}


@pytest.fixture
def conn(tmp_path):
    c = open_connection(tmp_path / "db.sqlite")
    apply_schema(c)
    hub.setup(c)
    yield c
    c.close()


def invoke(app, path, body=None, token=None, method="POST"):
    raw = json.dumps(body or {}).encode()
    status = []
    response = app({"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)),
                    "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(raw),
                    "REMOTE_ADDR": "127.0.0.1", "HTTP_AUTHORIZATION": "Bearer " + token if token else ""},
                   lambda value, headers: status.append(int(value.split()[0])))
    return status[0], json.loads(b"".join(response))


def test_reconciliation_actual_replaces_estimate_and_conflicts(conn):
    ledger.ingest(conn, "a", [event(), event(source="transcript", event_id="e2")])
    assert ledger.report(conn, "a")["accounted_nanos"] == 1000
    ledger.ingest(conn, "a", [event(source="billing", basis="actual", amount_nanos=900)])
    report = ledger.report(conn, "a")
    assert report["accounted_nanos"] == 900 and report["by_basis_nanos"]["estimated"] == 0
    ledger.ingest(conn, "a", [event(source="conflict", basis="actual", amount_nanos=800)])
    assert ledger.report(conn, "a")["conflicting_charges"] == 1
    assert ledger.report(conn, "a")["accounted_nanos"] == 0
    assert ledger.report(conn, "b")["accounted_nanos"] == 0


def test_idempotence_atomic_batches_and_quota(conn):
    assert ledger.ingest(conn, "a", [event()], quota=1) == 1
    assert ledger.ingest(conn, "a", [event()], quota=1) == 0
    with pytest.raises(ValueError):
        ledger.ingest(conn, "a", [event(event_id="new", request_id="new")], quota=1)
    with pytest.raises(ValueError):
        ledger.ingest(conn, "a", [event(event_id="new"), event(amount_nanos=42)])
    assert len(ledger.export(conn, "a")) == 1


@pytest.mark.parametrize("field,value", [("prompt", "SECRET"), ("arguments", {"token": "SECRET"}),
    ("workspace", "victim"), ("model", "has spaces SECRET"), ("amount_nanos", -1),
    ("input_tokens", True), ("schema_version", True), ("output_tokens", 10**18)])
def test_allowlist_and_bounded_types(conn, field, value):
    with pytest.raises(ValueError):
        ledger.ingest(conn, "a", [event(**{field: value})])
    assert ledger.export(conn, "a") == []


def test_delete_blocks_replay_and_respects_tenant(conn):
    ledger.ingest(conn, "a", [event()])
    ledger.ingest(conn, "b", [event()])
    assert ledger.delete(conn, "a") == 1
    assert ledger.ingest(conn, "a", [event(source="other")]) == 0
    assert ledger.export(conn, "a") == [] and len(ledger.export(conn, "b")) == 1


def test_receiver_isolation_revocation_and_no_scope_injection(tmp_path):
    path = tmp_path / "hub.db"
    app = hub.Application(path)
    c = open_connection(path)
    da, ta = hub.provision(c, "a")
    _, tb = hub.provision(c, "b")
    _, admin = hub.provision(c, "a", role="admin")
    assert invoke(app, "/v1/events", {"events": [event()]}, ta)[0] == 200
    assert invoke(app, "/v1/events", {"events": [event()], "workspace": "a"}, tb)[0] == 400
    assert invoke(app, "/v1/report", token=tb, method="GET")[0] == 403
    assert invoke(app, "/v1/report", token=admin, method="GET")[1]["accounted_nanos"] == 1000
    assert invoke(app, "/v1/delete", {"confirm": "delete-workspace"}, ta)[0] == 403
    hub.revoke(c, "a", da)
    assert invoke(app, "/v1/events", {"events": [event()]}, ta)[0] == 401
    assert ledger.export(c, "b") == []
    raw = path.read_bytes()
    assert ta.encode() not in raw and tb.encode() not in raw
    c.close()


def test_pairing_single_use_and_expiry(tmp_path):
    path = tmp_path / "hub.db"
    app = hub.Application(path)
    c = open_connection(path)
    _, result = invoke(app, "/v1/pair/start")
    secret, code = result["device_secret"], result["user_code"]
    assert invoke(app, "/v1/pair/poll", {"device_secret": secret})[1] == {"pending": True}
    hub.approve(c, code, "a")
    _, paired = invoke(app, "/v1/pair/poll", {"device_secret": secret})
    assert paired["workspace"] == "a" and paired["token"]
    assert invoke(app, "/v1/pair/poll", {"device_secret": secret})[0] == 400
    c.close()


def test_custom_sdk_context_and_storage_failure(tmp_path):
    path = tmp_path / "sdk.db"
    with Tracker(path) as tracker:
        with task("feature-1", session_id="parent"):
            parent = tracker.context()
            with task("feature-1", parent_session_id="parent"):
                assert tracker.record(provider="openai", model="gpt-5", request_id="req1",
                                      input_tokens=10, output_tokens=2)
            assert tracker.context() == parent
        assert not tracker.record(provider="openai", model="gpt-5")
    stats = tracker.close()
    assert stats["written"] == 1 and stats["invalid"] == 1
    c = open_connection(path)
    assert ledger.report(c, "local")["priced_charges"] == 1
    c.close()
    bad = tmp_path / "directory"
    bad.mkdir()
    with Tracker(bad) as failed:
        with task("feature-1"):
            failed.record(provider="openai", model="gpt-5", input_tokens=1)
    assert failed.close()["persist_failures"] == 1


def test_real_http_two_devices_durable_sync(tmp_path, monkeypatch):
    hub_path = tmp_path / "hub.db"
    app = hub.Application(hub_path)
    hc = open_connection(hub_path)
    _, token = hub.provision(hc, "team")
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(sync, "credential", lambda endpoint: token)
    try:
        for name in ("device1", "device2"):
            c = open_connection(tmp_path / (name + ".db"))
            ledger.setup(c)
            ledger.ingest(c, "local", [event(source=name)])
            sync.setting(c, "endpoint", endpoint)
            assert sync.push(c) == {"uploaded": 1}
            assert sync.push(c) == {"uploaded": 0}
            c.execute("DELETE FROM ledger_outbox")  # lost acknowledgement => safe replay
            assert sync.push(c) == {"uploaded": 1}
            c.close()
        assert ledger.report(hc, "team")["accounted_nanos"] == 1000
        assert len(ledger.export(hc, "team")) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        hc.close()


def test_sync_backoff_offline_queue(conn, monkeypatch):
    ledger.ingest(conn, "local", [event()])
    sync.setting(conn, "endpoint", "https://example.invalid")
    monkeypatch.setattr(sync, "credential", lambda endpoint: "token")
    monkeypatch.setattr(sync, "request", lambda *args: (_ for _ in ()).throw(OSError("SECRET")))
    result = sync.push(conn)
    assert result["retry_in_seconds"] > 0 and "SECRET" not in str(result)
    assert len(ledger.export(conn, "local")) == 1
    assert sync.push(conn)["backoff"]


@pytest.mark.parametrize("endpoint", ["http://example.com", "https://user:password@example.com",
    "https://example.com/?token=SECRET", "file:///tmp/anything"])
def test_endpoint_restrictions(endpoint):
    with pytest.raises(ValueError):
        sync.endpoint_url(endpoint)


def test_reviewed_nested_prefix_listing_labels_and_repair(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    root = Rollout(tmp_path / "cx")
    child = Rollout(tmp_path / "cx", parent=root, history_start_ordinal=6)
    grand = Rollout(tmp_path / "cx", parent=child)
    root.user_turn()
    root.usage(1000, 0, 10)
    child.append("event_msg", {"type": "token_count", "info": {
        "total_token_usage": dict(root.total), "last_token_usage": dict(root.last)}}, ordinal=4)
    child.ordinal = 6
    child.user_turn()
    child.usage(200, 0, 7)
    records = [json.loads(line) for line in child.path.read_text().splitlines()]
    for row in records:
        row.pop("ordinal", None)
    child.path.write_text("".join(json.dumps(row) + "\n" for row in records))
    grand.user_turn()
    grand.usage(100, 0, 1)
    ing = Ingestor(conn)
    for tf in codex.discover(CWD):
        ing.ingest(tf)
    assert conn.execute("SELECT COUNT(*) FROM agent_usage WHERE session_id=?", (child.thread_id,)).fetchone()[0] == 0
    wid = work.create(conn, "review", "task")
    work.assign(conn, wid, [root.thread_id])
    rep = work.build_report(conn, wid)
    assert rep.subagent_count == 2 and rep.coverage == "partial"
    assert "ambiguous subagent" in work.render_report(rep)
    listing = render_sessions(conn, CWD, include_empty=True)
    assert grand.thread_id[:8] in listing
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.execute("UPDATE agent_usage SET usage_id=session_id || ':l123456' WHERE session_id=?", (root.thread_id,))
    tf = next(f for f in codex.discover(CWD) if f.session_id == root.thread_id)
    with pytest.raises(ValueError, match="repair"):
        Ingestor(conn).ingest(tf)
    backup = recovery.repair(path)
    assert backup.exists()
    assert work.effective_assignment(conn, root.thread_id) == (wid, "explicit")
    assert conn.execute("SELECT COUNT(*) FROM agent_usage WHERE usage_id GLOB '*:l[0-9]*'").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(input_tokens) FROM agent_usage").fetchone()[0] == 1100


def test_bridge_does_not_export_content_paths_or_work_names(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    r = Rollout(tmp_path / "cx")
    r.user_turn()
    r.exec("SECRET_COMMAND")
    r.usage(100, 0, 1)
    Ingestor(conn).ingest(codex.discover(CWD)[0])
    wid = work.create(conn, "SECRET_WORK_NAME", "task", repository="/SECRET/PATH")
    work.assign(conn, wid, [r.thread_id])
    assert collection.collect(conn)["inserted"] == 1
    payload = json.dumps(ledger.export(conn, "local"))
    assert "SECRET" not in payload and CWD not in payload
    assert r.thread_id not in payload


def test_private_database_permissions(tmp_path):
    path = tmp_path / "db"
    c = open_connection(path)
    assert os.stat(path).st_mode & 0o077 == 0
    c.close()


def test_zero_cost_share_full_hash_trace_and_explicit_subagent(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    root = Rollout(tmp_path / "cx")
    child = Rollout(tmp_path / "cx", parent=root)
    child.user_turn()
    child.usage(100, 0, 1)
    records = [json.loads(line) for line in child.path.read_text().splitlines()]
    for record in records:
        record.pop("ordinal", None)
    child.path.write_text("".join(json.dumps(r) + "\n" for r in records))
    ing = Ingestor(conn)
    for tf in codex.discover(CWD):
        ing.ingest(tf)
    wid = work.create(conn, "zero", "task")
    work.assign(conn, wid, [child.thread_id])
    conn.execute("UPDATE agent_usage SET api_equiv_nanos=0,api_equiv_status='priced'")
    report = work.build_report(conn, wid)
    assert report.subagent_count == 0
    text = work.render_report(report, trace=5, conn=conn)
    assert "unknown" not in next(line for line in text.splitlines() if line.strip().startswith("Codex"))
    usage_id = conn.execute("SELECT usage_id FROM agent_usage").fetchone()[0]
    assert usage_id in text


def test_conflicting_attribution_requires_explicit_resolution(conn):
    ledger.ingest(conn, "a", [event(), event(source="transcript", work_item_id="feature-2")])
    assert ledger.report(conn, "a")["conflicting_charges"] == 1
    key = ledger.charge_key(event())
    ledger.assign(conn, "a", key, "feature-2")
    assert ledger.report(conn, "a", "feature-2")["accounted_nanos"] == 1000
    assert ledger.report(conn, "a", "feature-1")["accounted_nanos"] == 0
    with pytest.raises(ValueError):
        ledger.assign(conn, "b", key, "feature-1")


def test_first_run_cli_and_pause_resume(tmp_path, monkeypatch, capsys):
    from runpeek.cli import main
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    monkeypatch.setenv("RUNPEEK_CLAUDE_HOME", str(tmp_path / "cc"))
    rollout = Rollout(tmp_path / "cx")
    rollout.user_turn()
    rollout.usage(100, 0, 2)
    db = str(tmp_path / "onboard.db")
    assert main(["init", "--project", CWD, "--name", "First task", "--yes", "--db", db]) == 0
    assert "ACCOUNTED COST FOR THIS TASK" in capsys.readouterr().out
    assert main(["collect", "--db", db]) == 0
    assert "ACCOUNTED COST" in capsys.readouterr().out
    assert main(["pause", "--db", db]) == 0
    rollout.usage(100, 0, 2)
    assert main(["collect", "--db", db]) == 0
    assert "paused" in capsys.readouterr().out
    c = open_connection(db)
    assert c.execute("SELECT COUNT(*) FROM agent_usage").fetchone()[0] == 1
    c.close()
    assert main(["resume", "--db", db]) == 0
    assert main(["collect", "--db", db]) == 0
    c = open_connection(db)
    assert c.execute("SELECT COUNT(*) FROM agent_usage").fetchone()[0] == 2
    c.close()
    assert main(["join", "AUTH-42", "--db", db]) == 0


def test_receiver_limits_and_no_redirect_token_leak(tmp_path):
    app = hub.Application(tmp_path / "hub.db")
    statuses = []
    response = app({"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/events", "CONTENT_LENGTH": "99999999",
                    "CONTENT_TYPE": "application/json", "REMOTE_ADDR": "127.0.0.1", "wsgi.input": io.BytesIO()},
                   lambda value, headers: statuses.append(value))
    assert statuses[0].startswith("413") and b"Payload too large" in b"".join(response)
    for _ in range(5):
        assert invoke(app, "/v1/pair/start")[0] == 200
    assert invoke(app, "/v1/pair/start")[0] == 429


def test_watcher_nested_live_append(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    root = Rollout(tmp_path / "cx")
    child = Rollout(tmp_path / "cx", parent=root)
    grand = Rollout(tmp_path / "cx", parent=child)
    ing = Ingestor(conn)
    for r in (root, child, grand):
        r.user_turn()
    files = codex.discover(CWD)
    for tf in files:
        ing.ingest(tf)
    grand.usage(100, 0, 1)
    for tf in files:
        ing.ingest(tf)
    wid = work.create(conn, "live nested", "task")
    work.assign(conn, wid, [root.thread_id])
    assert work.build_report(conn, wid).total.calls == 1
    assert grand.thread_id[:8] in render_sessions(conn, CWD, include_empty=True)


def test_receiver_corrections_require_observed_charge(tmp_path):
    path = tmp_path / "hub.db"
    app = hub.Application(path)
    c = open_connection(path)
    _, owner = hub.provision(c, "a")
    _, other = hub.provision(c, "a")
    invoke(app, "/v1/events", {"events": [event()]}, owner)
    body = {"charge_key": ledger.charge_key(event()), "work_item_id": "feature-2"}
    assert invoke(app, "/v1/assign", body, other)[0] == 403
    assert invoke(app, "/v1/assign", body, owner)[0] == 200
    assert ledger.report(c, "a", "feature-2")["accounted_nanos"] == 1000
    c.close()


def test_sdk_bounded_queue_reports_drops(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = ledger.ingest

    def slow(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "ingest", slow)
    tracker = Tracker(tmp_path / "sdk.db", capacity=1)
    try:
        with task("feature"):
            assert tracker.record(provider="openai", model="gpt-5", input_tokens=1)
            assert entered.wait(5)
            assert tracker.record(provider="openai", model="gpt-5", input_tokens=1)
            assert not tracker.record(provider="openai", model="gpt-5", input_tokens=1)
    finally:
        release.set()
    assert tracker.close()["dropped"] == 1


def test_sync_carries_attribution_correction(conn, monkeypatch):
    ledger.ingest(conn, "local", [event()])
    ledger.assign(conn, "local", ledger.charge_key(event()), "feature-2")
    sync.setting(conn, "endpoint", "https://example.invalid")
    monkeypatch.setattr(sync, "credential", lambda endpoint: "token")
    paths = []

    def acknowledge(endpoint, path, body, token):
        paths.append(path)
        return {"inserted": 1} if path == "/v1/events" else {"assigned": True}

    monkeypatch.setattr(sync, "request", acknowledge)
    assert sync.push(conn) == {"uploaded": 1}
    assert paths == ["/v1/events", "/v1/assign"]
    sync.push(conn)
    assert len(paths) == 2


def test_repair_missing_source_leaves_data_unchanged(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    r = Rollout(tmp_path / "cx")
    r.user_turn()
    r.usage(10, 0, 1)
    Ingestor(conn).ingest(codex.discover(CWD)[0])
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    r.path.unlink()
    with pytest.raises(ValueError, match="available"):
        recovery.repair(path)
    assert conn.execute("SELECT SUM(input_tokens) FROM agent_usage").fetchone()[0] == 10


@pytest.mark.parametrize("model,category", [("gpt-5", "priced_charges"), ("gpt-5.5", "unknown_charges")])
def test_bridge_reassignment_back_to_original(conn, tmp_path, monkeypatch, model, category):
    monkeypatch.setenv("RUNPEEK_CODEX_HOME", str(tmp_path / "cx"))
    r = Rollout(tmp_path / "cx", model=model)
    r.user_turn()
    r.usage(100, 0, 1)
    Ingestor(conn).ingest(codex.discover(CWD)[0])
    first, second = work.create(conn, "first", "task"), work.create(conn, "second", "task")
    for wid in (first, second, first):
        work.assign(conn, wid, [r.thread_id])
        assert collection.collect(conn)["conflicting_or_invalid"] == 0
        assert ledger.report(conn, "local", wid)[category] == 1
        other = second if wid == first else first
        assert ledger.report(conn, "local", other)[category] == 0
