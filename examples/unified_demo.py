"""Synthetic two-device demo. No model calls, credentials, or external service needed."""
from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

from runpeek import hub, ledger, sync
from runpeek.store import open_connection
from runpeek.tracking import Tracker, task


class Quiet(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="runpeek-demo-") as tmp:
        root = Path(tmp)
        app = hub.Application(root / "hub.db")
        hc = open_connection(root / "hub.db")
        server = make_server("127.0.0.1", 0, app, handler_class=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        try:
            for device in ("laptop", "workstation"):
                _, credential = hub.provision(hc, "demo-team")
                db = root / (device + ".db")
                with Tracker(db, agent="custom", source=device) as tracker, task("AUTH-42"):
                    tracker.record(provider="openai", model="gpt-5", request_id="shared-request",
                                   input_tokens=1000, output_tokens=100, amount_nanos=2_000_000_000)
                conn = open_connection(db)
                events = ledger.export(conn, "local")
                sync.request(endpoint, "/v1/events", {"events": events}, credential)
                sync.request(endpoint, "/v1/events", {"events": events}, credential)
                conn.close()
            invoice = dict(events[0], source="billing", event_id="invoice-line", basis="actual",
                           amount_nanos=1_500_000_000)
            sync.request(endpoint, "/v1/events", {"events": [invoice]}, credential)
            result = ledger.report(hc, "demo-team", "AUTH-42")
            assert result["accounted_nanos"] == 1_500_000_000
            assert result["priced_charges"] == 1
            print("SYNTHETIC DEMO — two devices + billing, repeated delivery, one reconciled charge")
            print(ledger.render(result))
            print(json.dumps(result, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            hc.close()


if __name__ == "__main__":
    main()
