import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import healthwatch as watch


class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz/ready":
            if getattr(self.server, "redirect", False):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            body = {"status": "ready", "checks": [{"name": "database", "required": True, "ok": True}]}
        elif self.path == "/api/v1/market/status":
            if getattr(self.server, "missing", False):
                self.send_error(404)
                return
            body = {"state": "ready", "source": "replay", "instruments": [
                {"instrument": "SPY.ARCA", "age_seconds": 4, "stale": False},
                {"instrument": "AAPL.NASDAQ", "age_seconds": 120, "stale": self.server.stale},
            ]}
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.posts.append((self.headers["Idempotency-Key"], received))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


@contextmanager
def serving(tls=None, port=0):
    server = ReusableServer(("127.0.0.1", port), Api)
    server.stale = False
    server.posts = []
    if tls:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def flow(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class HealthwatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for folder in ("run", "logs/healthwatch", "secrets"):
            (self.root / folder).mkdir(parents=True)
        self.alerts = watch.Alerts(self.root/"alerts.jsonl")
        self.dt = datetime(2026, 1, 5, 6, tzinfo=timezone.utc)

    def tls(self):
        cert, key = (self.root / s for s in ("cert.pem", "key.pem"))
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-days", "1", "-nodes",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", key, "-out", cert], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        env = patch.dict(os.environ, {"SSL_CERT_FILE": str(cert), "no_proxy": "127.0.0.1,localhost"})
        env.start()
        self.addCleanup(env.stop)
        return context

    def test_durable_transition_dedup_and_recovery(self):
        key = "service:finance-api:ready"
        def step(seconds, ok, journal):
            journal.notify(self.dt + timedelta(seconds=seconds), {key: watch.measured(ok, "fixture")})
        step(0, True, self.alerts)
        step(1, False, self.alerts)
        step(31, False, self.alerts)
        restarted = watch.Alerts(self.alerts.path)
        step(50, False, restarted)
        step(55, True, restarted)
        step(60, False, restarted)
        step(61, True, restarted)
        step(62, False, restarted)
        step(901, False, restarted)
        step(910, True, restarted)
        step(960, True, restarted)
        alerts = flow(self.alerts.path)
        self.assertEqual([r["event"] for r in alerts], ["failure", "recovery", "failure", "recovery"])
        self.assertEqual(len({r["id"] for r in alerts}), 4)

    def test_public_https_failure_is_independent_and_requires_a_real_json_ready(self):
        tls = self.tls()
        with serving() as local:
            rows = {"finance-api": {"enabled": True, "ready_url": f"http://127.0.0.1:{local.server_port}/healthz/ready",
                                    "state": "RUNNING", "pid": 123, "live_url": None, "ready": 200}}
            def sample(origin):
                checks = watch.service_checks({"supervisor": "RUNNING", "services": rows})
                checks["public:ready"] = watch.public_ready(origin, timeout=1)
                self.assertEqual(checks["service:finance-api:ready"]["state"], "ok")
                self.alerts.notify(watch.now(), checks)
                return checks["public:ready"]["state"]
            with serving(tls) as public:
                port = public.server_port
                origin = f"https://127.0.0.1:{port}"
                self.assertEqual(sample(origin), "ok")
            self.assertEqual(sample(origin), "failed")
            self.assertEqual(sample(origin), "failed")
            with serving(tls, port) as public:
                self.assertEqual(sample(origin), "ok")
                public.redirect = True
                self.assertEqual(watch.public_ready(origin, timeout=1)["state"], "failed")
            self.assertEqual([(x["check"], x["event"]) for x in flow(self.alerts.path)],
                             [("public:ready", "failure"), ("public:ready", "recovery")])
            self.assertEqual(watch.public_ready(None, 1)["state"], "skipped")

    def test_settings_and_independent_disk_failure(self):
        config = self.root / "watch.yaml"
        config.write_text("public_origin: https://test.invalid\nbackup_at_utc: null\n")
        with patch.dict(os.environ, {"FERTILE_HEALTH_CONFIG": str(config), "FERTILE_PUBLIC_ORIGIN": "https://chosen.invalid"}):
            conf = watch.settings(self.root)
        self.assertEqual(conf["public_origin"], "https://chosen.invalid")
        conf.update(disk_path=str(self.root / "missing"), public_origin=None)
        observer = watch.Delivery(self.root, self.alerts.path)
        rows = {
            "supervisor": "RUNNING", "services": {"web": {"enabled": True, "state": "RUNNING",
                "pid": 100, "live_url": "http://localhost/healthz/live", "live": 200,
                "ready_url": "http://localhost/healthz/ready", "ready": 200}}}
        with patch("healthwatch.services", return_value=rows):
            checks = watch.observe(self.root, conf, watch.Backup(self.root), observer, once=True)
        self.assertEqual(checks["disk:free"]["state"], "failed")
        self.assertEqual(checks["service:web:ready"]["state"], "ok")
        self.assertEqual(checks["public:ready"]["state"], "skipped")
        self.assertEqual(checks["backup:age"]["state"], "failed")
        self.assertEqual(checks["healthwatch:runtime"]["state"], "ok")

    def test_market_age_missing_endpoint_and_disabled_collector(self):
        with serving() as api:
            rows = {"finance-api": {"enabled": True, "state": "RUNNING",
                                    "ready_url": f"http://127.0.0.1:{api.server_port}/healthz/ready"}}
            self.assertEqual(watch.market(rows, 1)["max_age_seconds"], 120)
            self.assertEqual(watch.market(rows, 1)["state"], "ok")
            api.stale = True
            self.assertEqual(watch.market(rows, 1)["state"], "failed")
            api.missing = True
            self.assertEqual(watch.market(rows, 1)["state"], "skipped")
            rows["finance-collector"] = {"enabled": False, "state": "disabled"}
            self.assertEqual(watch.market(rows, 1)["state"], "skipped")

    def test_optional_webhook_uses_tls_replays_cursor_and_never_logs_secret(self):
        tls = self.tls()
        sender = watch.Delivery(self.root, self.alerts.path)
        with serving(tls) as remote:
            path = self.root/"secrets/alerts.env"
            path.write_text(f"ALERT_WEBHOOK_URL=https://127.0.0.1:{remote.server_port}/private-token\n")
            path.chmod(0o644)
            self.alerts.notify(self.dt, {"test": watch.measured(False, "unreachable")})
            sender.drain(self.root, 1)
            self.assertEqual(sender.result["state"], "failed")
            self.assertEqual(remote.posts, [])
            path.chmod(0o600)
            restarted = watch.Delivery(self.root, self.alerts.path)
            restarted.drain(self.root, 1)
            restarted.drain(self.root, 1)
            self.assertEqual(len(remote.posts), 1)
            self.assertEqual(remote.posts[0][0], flow(self.alerts.path)[0]["id"])
            path.unlink()
            self.alerts.notify(self.dt + timedelta(seconds=1), {"test": watch.measured(True, "restored")})
            restarted.drain(self.root, 1)
            self.assertEqual(restarted.result["state"], "skipped")
            self.assertNotIn("private-token", self.alerts.path.read_text())

    def test_backup_scheduler_only_counts_finished_snapshots(self):
        root = self.root
        due = self.dt
        dated = root/"backups"/due.strftime("%Y%m%dT%H%M%S.%fZ")
        dated.mkdir(parents=True)
        (dated/"snapshot.json").write_text("{}")
        self.assertIsNone(watch.latest_backup(root))
        (dated/"SHA256SUMS").write_text("fixture")
        self.assertEqual(watch.latest_backup(root), due)
        b = watch.Backup(root)
        with patch("healthwatch.subprocess.Popen") as launch:
            self.assertEqual(b.schedule({"backup_at_utc": "03:00"}, due, due, False)["state"], "ok")
            launch.assert_not_called()
            later = due + timedelta(days=1)
            b.schedule({"backup_at_utc": "03:00"}, later, due, False)
            launch.assert_called_once()
            launch.return_value.poll.return_value = 2
            self.assertEqual(b.schedule({"backup_at_utc": "03:00"}, later, due, False)["state"], "failed")
            b.schedule({"backup_at_utc": "03:00"}, later + timedelta(seconds=60), due, False)
            self.assertEqual(launch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
