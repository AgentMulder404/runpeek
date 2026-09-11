"""Minimal OTLP/HTTP receiver for probes: accepts JSON (and stores raw protobuf bytes) on /v1/logs, /v1/metrics, /v1/traces.
Writes every request to a JSONL file: {"path","content_type","body"} (body decoded when JSON)."""
import json, sys, gzip
from http.server import BaseHTTPRequestHandler, HTTPServer
out = open(sys.argv[2], "a")
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(n)
        if self.headers.get("Content-Encoding") == "gzip": raw = gzip.decompress(raw)
        ct = self.headers.get("Content-Type", "")
        body = None
        if "json" in ct:
            try: body = json.loads(raw)
            except Exception: body = {"_undecodable": len(raw)}
        else: body = {"_binary_bytes": len(raw)}
        out.write(json.dumps({"path": self.path, "content_type": ct, "body": body}) + "\n"); out.flush()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
