"""Regression tests for the acceptance script's evidence and credential boundaries."""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
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
from unittest.mock import patch

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


class PaperSmokeTests(unittest.TestCase):
    def setUp(self):
        self.services = {name: {"state": "RUNNING", "pid": 123} for name in
                         ("postgres", "finance-api", "finance-collector", "finance-backtest-worker", "web")}
        for name in ("finance-api", "finance-collector", "web"):
            self.services[name].update(live_url="http://127.0.0.1:8090/healthz/live", live=200,
                                       ready_url="http://127.0.0.1:8090/healthz/ready", ready=200)
        self.services["finance-collector"]["ready"] = 503
        logger = patch.object(check, "log")
        logger.start()
        self.addCleanup(logger.stop)

    def snapshot(self):
        return json.dumps({"supervisor": "RUNNING", "services": self.services})

    def await_local(self, **options):
        with patch.object(check.subprocess, "check_output", return_value=self.snapshot()), \
             patch.object(check.time, "monotonic", side_effect=[0, 61]):
            return check.await_stack(**options)

    def test_paper_stack_accepts_collector_503_with_healthwatch_absent_or_stopped(self):
        self.assertEqual(self.await_local(paper_only=True), ("http://127.0.0.1:8090", {}))
        for name in ("healthwatch", "ib-gateway", "xvfb"):
            self.services[name] = {"state": "STOPPED", "pid": 0}
        _, skipped = self.await_local(paper_only=True)
        self.assertEqual(skipped, dict.fromkeys(("healthwatch", "ib-gateway", "xvfb"), "STOPPED"))

    def test_strict_stack_requires_healthwatch_and_collector_ready(self):
        with self.assertRaisesRegex(check.SmokeError, "missing a required service"):
            self.await_local()
        self.services["healthwatch"] = {"state": "STOPPED", "pid": 0}
        with self.assertRaisesRegex(check.SmokeError, "healthwatch=STOPPED"):
            self.await_local()
        self.services["healthwatch"].update(state="RUNNING", pid=123)
        with self.assertRaisesRegex(check.SmokeError, "finance-collector=.*ready=503"):
            self.await_local()
        self.services["finance-collector"]["ready"] = 200
        self.assertEqual(self.await_local(), ("http://127.0.0.1:8090", {}))

    def test_paper_stack_still_requires_core_services_and_collector_live(self):
        for name in ("finance-api", "web"):
            with self.subTest(name=name):
                service = self.services.pop(name)
                with self.assertRaisesRegex(check.SmokeError, "missing a required service"):
                    self.await_local(paper_only=True)
                self.services[name] = service
        self.services["finance-collector"]["live"] = 503
        with self.assertRaisesRegex(check.SmokeError, "finance-collector=.*live=503"):
            self.await_local(paper_only=True)
        self.services["finance-collector"].update(live=200, ready=500)
        with self.assertRaisesRegex(check.SmokeError, "finance-collector=.*ready=500"):
            self.await_local(paper_only=True)

    def exercise_cli(self, *args, detail_state="recovering"):
        summary = {**check.JOB, "generated_at": check.JOB["end"], "run_id": "fixture", "total_orders": 0,
                   "total_positions": 0, "pnl": "0.00", "currency": "USD"}
        responses = {
            "/healthz/live": {"status": "ok"},
            "/healthz/ready": {"status": "ready", "checks": [{"ok": True}]},
            "/api/v1/system/status": {
                "web": {"version": "0.0.0", "commit": "c" * 40},
                "database": {"ok": True}, "finance": {"ok": True, "circuit": "closed"},
                "market": {"source": "ib", "state": "recovering", "fresh": False, "instrument_count": 1, "stale_count": 1},
            },
            "/api/v1/market/status": {"source": "ib", "state": detail_state, "instruments": [
                {"instrument": "SPY.ARCA", "age_seconds": None, "last_bar_at": None, "stale": True},
            ]},
            "/api/v1/backtests/sample": summary,
            "/api/v1/backtests": {"id": 5},
            "/api/v1/backtests/5": {"id": 5, "request": check.JOB, "status": "succeeded", "summary": summary},
        }
        assets = {"/": ("text/html", b'<script type="module" src="/a.js"></script><link rel="stylesheet" href="/a.css">'),
                  "/a.js": ("text/javascript", b"export default 1"), "/a.css": ("text/css", b"body{}")}
        requests = []
        version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()

        def respond(request, *, timeout):
            self.assertEqual(request.get_header("User-agent"), f"fertile-e2e-smoke/{version}")
            requests.append((request.host, request.selector, request.get_method()))
            if request.selector in assets:
                media, body = assets[request.selector]
            else:
                media, body = "application/json", json.dumps(responses[request.selector]).encode()
            response = io.BytesIO(body)
            response.status = 202 if request.get_method() == "POST" else 200
            response.headers = Message()
            response.headers["Content-Type"] = media
            return response

        with patch.object(sys, "argv", [check.__file__, *args]), \
             patch.object(check.subprocess, "check_output", return_value=self.snapshot()) as status, \
             patch.object(check, "token_from", return_value="fixture"), \
             patch.object(check, "build_opener") as build_opener:
            build_opener.return_value.open.side_effect = respond
            check.main()
            if args[0] == "public":
                status.assert_not_called()
        return requests

    def test_paper_local_and_public_run_the_full_smoke_with_user_agent(self):
        for args, host in ((["local"], "127.0.0.1:8090"), (["public", check.PUBLIC_ORIGIN], "fertile.viramoon.com")):
            with self.subTest(args=args):
                requests = self.exercise_cli(*args, "--paper-only")
                self.assertIn((host, "/api/v1/backtests", "POST"), requests)
                self.assertIn((host, "/a.js", "GET"), requests)
                self.assertIn((host, "/a.css", "GET"), requests)

    def test_strict_http_rejects_recovery_and_paper_rejects_disagreement(self):
        with self.assertRaisesRegex(check.SmokeError, "unhealthy system market summary"):
            self.exercise_cli("public", check.PUBLIC_ORIGIN)
        with self.assertRaisesRegex(check.SmokeError, "inconsistent recovering market status"):
            self.exercise_cli("public", check.PUBLIC_ORIGIN, "--paper-only", detail_state="degraded")


if __name__ == "__main__":
    unittest.main()
