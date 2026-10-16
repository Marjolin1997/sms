"""Upstream i rremë për testin e skajit (M11-I/F-02): shfaq çfarë mori nga nginx dhe përgjigjet me header-a "si aplikacioni".

Dëgjon 8000 (alias `api`) dhe 8001 (alias `central-api`). Vetëm test lokal; asnjë sekret, asnjë rrjet i jashtëm.
"""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _do(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        got = {k.lower(): v for k, v in self.headers.items()}
        out = json.dumps(
            {
                "path": self.path.split("?", 1)[0],
                "query": self.path.split("?", 1)[1] if "?" in self.path else "",
                "method": self.command,
                "body_len": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "headers": got,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        # header-a "si aplikacioni": nginx duhet t'i heqë/mos t'i dyfishojë sipas rregullave
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Strict-Transport-Security", "max-age=1")
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = _do

    def log_message(self, *a):
        pass


def serve(port):
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    for p in (8001,):
        threading.Thread(target=serve, args=(p,), daemon=True).start()
    serve(8000)
