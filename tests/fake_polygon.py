"""A stand-in for polygon.codeforces.com/api, enough for Outpost's import.

It checks every request's signature from Polygon's documented recipe
(written separately from outpost.polygon_api.sign), answers the first call
with an HTML page the way a rate-limited Polygon does, and serves packages
built by real_packages.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY, SECRET = "test-key", "test-secret"


class FakePolygon:
    def __init__(self, packages: dict[str, bytes]):
        """``packages``: problem name -> package zip bytes."""
        self.problems = [{"id": 1000 + n, "name": name, "owner": "someone", "revision": 7,
                          "latestPackage": 7, "accessType": "OWNER", "deleted": False}
                         for n, name in enumerate(sorted(packages))]
        self.zips = {1000 + n: packages[name] for n, name in enumerate(sorted(packages))}
        self.calls: list[str] = []
        self.rate_limit_next = True
        self.built: list[int] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                params = dict(urllib.parse.parse_qsl(body))
                fake.calls.append(method)
                if fake.rate_limit_next:
                    fake.rate_limit_next = False
                    return self._send(429, b"<html><body>Too many requests</body></html>", "text/html")
                if not fake.signature_ok(method, params):
                    return self._json({"status": "FAILED", "comment": "apiSig: Incorrect signature"}, 400)
                return fake.handle(self, method, params)

            def _send(self, code, data, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode(), "application/json")

        self.Handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def signature_ok(self, method: str, params: dict) -> bool:
        sig = params.pop("apiSig", "")
        if params.get("apiKey") != KEY or len(sig) != 6 + 128:
            return False
        rand = sig[:6]
        pairs = sorted(params.items())
        text = rand + "/" + method + "?" + "&".join(k + "=" + v for k, v in pairs) + "#" + SECRET
        return sig == rand + hashlib.sha512(text.encode()).hexdigest()

    def handle(self, h, method: str, params: dict):
        pid = int(params.get("problemId", 0) or 0)
        if method == "problems.list":
            return h._json({"status": "OK", "result": self.problems})
        if pid not in self.zips:
            return h._json({"status": "FAILED", "comment": "problemId: no such problem"}, 400)
        if method == "problem.packages":
            return h._json({"status": "OK", "result": [
                {"id": 5, "revision": 6, "state": "READY", "type": "standard", "comment": ""},
                {"id": 9, "revision": 7, "state": "READY", "type": "linux", "comment": ""},
                {"id": 11, "revision": 7, "state": "RUNNING", "type": "linux", "comment": ""}]})
        if method == "problem.package":
            if params.get("packageId") != "9":
                return h._json({"status": "FAILED", "comment": "packageId: not the newest ready one"}, 400)
            return h._send(200, self.zips[pid], "application/zip")
        if method == "problem.buildPackage":
            self.built.append(pid)
            return h._json({"status": "OK", "result": None})
        return h._json({"status": "FAILED", "comment": f"unknown method {method}"}, 400)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
