"""Regression tests for the acceptance script's evidence and credential boundaries."""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest

import e2e_smoke as check


class Site(BaseHTTPRequestHandler):
    def send(self, status, data, media="application/json"):
        body = json.dumps(data).encode() if media == "application/json" else data.encode()
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.reads.append((self.path, self.headers.get("Authorization") is not None))
        if self.path == "/":
            self.send(200, '<script type="module" src="/a.js"></script><link rel="stylesheet" href="/a.css">', "text/html")
        elif self.path == "/a.js":
            self.send(200, "export default 1", "text/javascript")
        elif self.path == "/a.css":
            self.send(200, "body{color:#222}", "text/css")
        elif self.path == "/healthz/live":
            self.send(200, {"status": "ok"})
        elif self.path == "/healthz/ready":
            self.send(200, {"status": "ready", "checks": [{"name": "database", "ok": True}]})
        elif self.path == "/api/v1/system/status":
            self.send(200, {"database": {"ok": True}, "finance": {"ok": True, "circuit": "closed"},
                            "web": {"version": "0.0.0", "commit": "c" * 40},
                            "market": {"fresh": True, "state": "ready", "instrument_count": 1,
                                       "stale_count": 0, "source": self.server.source}})
        elif self.path == "/api/v1/market/status":
            now = datetime.now(timezone.utc)
            self.send(200, {"state": "ready", "updated_at": now.isoformat(), "source": self.server.source, "max_age_seconds": 180,
                            "instruments": [{"instrument": "SPY.ARCA", "age_seconds": self.server.age,
                                             "last_bar_at": (now - timedelta(seconds=self.server.age)).isoformat(),
                                             "stale": False}]})
        elif self.path == "/api/v1/backtests/sample":
            self.send(200, self.server.summary)
        elif self.path == "/api/v1/backtests/5":
            self.send(200, {"id": 5, "request": check.JOB, "status": "succeeded", "summary": self.server.summary})
        else:
            self.send(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Authorization") != "Bearer " + self.server.token or json.loads(body) != check.JOB:
            self.send(401, {})
            return
        self.server.posts.append(self.headers.get("Idempotency-Key"))
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "/never-forward-here")
            self.end_headers()
        else:
            self.send(202, {"id": 5})

    def log_message(self, *_):
        pass


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        self.addCleanup(self.site.server_close)
        self.site.token = secrets.token_urlsafe(32)
        self.site.summary = {**check.JOB, "generated_at": check.JOB["end"], "run_id": "abc", "total_orders": 0,
                             "total_positions": 0, "pnl": "12345678901234567890.12345678", "currency": "USD"}
        self.site.redirect = False
        self.site.age = 5
        self.site.source = "replay"
        self.site.posts, self.site.reads = [], []
        threading.Thread(target=self.site.serve_forever, daemon=True).start()
        self.addCleanup(self.site.shutdown)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.secret = Path(directory.name) / "web.env"
        with os.fdopen(os.open(self.secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as handle:
            handle.write("WEB_ADMIN_TOKEN=" + self.site.token + "\n")
        self.http = check.HTTP(f"http://127.0.0.1:{self.site.server_port}", local=True)

    def exercise(self, protocol_fixture=False):
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                check.smoke(self.http, self.secret, {"ib-gateway": "disabled"}, protocol_fixture)
        finally:
            self.assertNotIn(self.site.token, output.getvalue(), "credential appeared in evidence")
            self.assertFalse(any(auth for _, auth in self.site.reads), "authenticated GET sent unexpectedly")
        return output.getvalue()

    def test_real_http_verifies_assets_precision_and_one_submission(self):
        self.assertIn("12345678901234567890.12345678", self.exercise())
        self.assertEqual(len(self.site.posts), 1)
        self.assertTrue(self.site.posts[0])
        self.assertIn(("/a.js", False), self.site.reads)
        self.assertIn(("/a.css", False), self.site.reads)

    def test_authenticated_redirect_is_not_followed(self):
        self.site.redirect = True
        with self.assertRaisesRegex(check.SmokeError, "HTTP 302"):
            self.exercise()
        self.assertEqual(len(self.site.posts), 1)
        self.assertNotIn("/never-forward-here", [p for p, _ in self.site.reads])

    def test_fixture_requires_ib_data_and_explicit_mode(self):
        with self.assertRaisesRegex(check.SmokeError, "requires the IB collector"):
            self.exercise(protocol_fixture=True)
        self.site.source = "ib-realtime"
        with self.assertRaisesRegex(check.SmokeError, "external services"):
            self.exercise()
        self.assertFalse(self.site.posts)
        self.assertIn("synthetic loopback IB protocol fixture", self.exercise(protocol_fixture=True))
        self.assertEqual(len(self.site.posts), 1)

    def test_fixture_rejects_credentials_and_public_switch(self):
        root = self.secret.parent
        (root / "secrets").mkdir()
        (root / "secrets/ibkr-paper.env").touch()
        with self.assertRaisesRegex(check.SmokeError, "paper credentials"):
            check.require_fixture_environment(root, root)
        proc = subprocess.run([sys.executable, check.__file__, "public", "https://fertile.viramoon.com",
                               "--protocol-fixture"], capture_output=True, timeout=3)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"unrecognized arguments", proc.stderr)

    def test_fixture_verifies_which_process_owns_the_socket(self):
        script = self.secret.parent / "peer.py"
        script.write_text("import socket, sys\ns = socket.socket()\ns.bind(('127.0.0.1', 0))\ns.listen()\n"
                          "print(s.getsockname()[1], flush=True)\nsys.stdin.read()\n")
        with subprocess.Popen([sys.executable, script, "gateway"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as peer:
            try:
                port = int(peer.stdout.readline())
                check.require_fixture_listener(peer.pid, port)
                with self.assertRaisesRegex(check.SmokeError, "does not own"):
                    check.require_fixture_listener(peer.pid, self.site.server_port)
            finally:
                peer.stdin.close()

    def test_expired_bars_and_numeric_pnl_stop_before_submission(self):
        self.site.age = 300
        with self.assertRaisesRegex(check.SmokeError, "stale"):
            self.exercise()
        self.site.age = 1
        self.site.summary["pnl"] = 123
        with self.assertRaisesRegex(check.SmokeError, "decimal JSON string"):
            self.exercise()
        self.assertFalse(self.site.posts)

    def test_explicit_origins_only_and_private_secret(self):
        self.assertEqual(check.public_origin("https://fertile.viramoon.com/"), "https://fertile.viramoon.com")
        for origin in ("http://fertile.viramoon.com", "https://fertile.viramoon.com.evil.invalid",
                       "https://fertile.viramoon.com/other", "https://account@fertile.viramoon.com",
                       "https://www.viramoon.com"):
            with self.assertRaises(argparse.ArgumentTypeError):
                check.public_origin(origin)
        self.secret.chmod(0o640)
        with self.assertRaisesRegex(check.SmokeError, "0600"):
            check.token_from(self.secret)
        with self.assertRaisesRegex(check.SmokeError, "cross-origin"):
            self.http.read("https://other.invalid/a.js", asset=True)


if __name__ == "__main__":
    unittest.main()
