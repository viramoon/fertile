"""Offline safety checks. Never contact IB or start the broker's application."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "scripts/ibgateway.py"
spec = importlib.util.spec_from_file_location("gateway", MODULE)
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)


class PaperGateway(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.root = root
        for name, path in {
            "ROOT": root, "RUN": root / "run", "LOG": root / "logs", "DATA": root / "data",
            "INI": root / "run/config.ini", "STATE": root / "run/state.json",
            "POLICY": root / "tools/ibgateway/loopback.so",
        }.items():
            guard = patch.object(gw, name, path)
            guard.start()
            self.addCleanup(guard.stop)
        for path in (gw.RUN, gw.LOG, gw.DATA):
            path.mkdir(mode=0o700)
        self.env = dict(os.environ, FERTILE_ROOT=str(root))

    def test_real_missing_credentials_guard(self):
        for command in ("run", "status", "xvfb", "check"):
            p = subprocess.run([REPO / "scripts/ibgateway.sh", command],
                               env=self.env, capture_output=True, check=False)
            self.assertEqual(p.returncode, 1)
            self.assertEqual(json.loads(p.stdout)["state"], "needs_credentials")
        saved = json.loads((self.root / "run/ibgateway/state.json").read_text())
        self.assertEqual(saved["attempts"], 0)
        self.assertFalse((self.root / "run/ibgateway/config.ini").exists())

    def test_private_configuration_preserves_jts_and_literal_values(self):
        secret = self.root / "secrets/ibkr-paper.env"
        secret.parent.mkdir()
        secret.write_text("IBKR_USERNAME=test\nIBKR_PASSWORD=example:=\\ 保\n")
        with patch.object(gw, "SECRET", secret):
            secret.chmod(0o644)
            with self.assertRaisesRegex(gw.GatewayError, "invalid_credentials_permissions"):
                gw.credentials(True)
            secret.chmod(0o600)
            (gw.DATA / "jts.ini").write_text("[Other]\nKeep=yes\n")
            gw.configure(gw.credentials(True))
            self.assertEqual(gw.INI.stat().st_mode & 0o777, 0o600)
            config = gw.INI.read_text()
            self.assertIn("IbPassword=" + gw.property_value("example:=\\ 保"), config)
            self.assertIn("TradingMode=paper\nReadOnlyApi=yes\nOverrideTwsApiPort=4002", config)
            self.assertIn("Keep=yes", (gw.DATA / "jts.ini").read_text())
            secret.unlink()
            secret.symlink_to(gw.INI)
            with self.assertRaisesRegex(gw.GatewayError, "invalid_credentials_permissions"):
                gw.credentials(True)

    def test_kernel_loopback_policy_and_scope(self):
        gw.POLICY.parent.mkdir(parents=True)
        subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        REPO / "deploy/ibgateway/loopback.c", "-ldl", "-o", gw.POLICY], check=True)
        env = dict(self.env, LD_PRELOAD=str(gw.POLICY))
        subprocess.run([sys.executable, MODULE, "--verify-bind"], env=env, check=True)
        snippet = """import socket
s=socket.socket()
s.bind(('0.0.0.0', 4002))
s.listen()
print(s.getsockname()[0], flush=True)
input()
"""
        for environment, expected in ((env, "127.0.0.1"), (self.env, "0.0.0.0")):
            with subprocess.Popen([sys.executable, "-c", snippet], env=environment, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE) as p:
                try:
                    self.assertEqual(p.stdout.readline().strip(), expected)
                    if expected == "127.0.0.1":
                        self.assertTrue(gw.listeners())
                    else:
                        with self.assertRaisesRegex(gw.GatewayError, "unsafe_listener"):
                            gw.listeners()
                finally:
                    p.communicate("\n")
        # A missing preload library must fail the same preflight instead of starting IBC.
        missing = subprocess.run([sys.executable, MODULE, "--verify-bind"], capture_output=True,
                                 env=dict(self.env, LD_PRELOAD=str(gw.POLICY) + ".missing"))
        self.assertNotEqual(missing.returncode, 0)

    def test_retry_budget_survives_launcher_restart_and_requires_operator_reset(self):
        gw.state("startup", gw.BUDGET)
        # Stop the test at its first park. No vendor login or X server is started.
        with patch.object(gw.time, "sleep", side_effect=InterruptedError),              patch.object(gw, "credentials", side_effect=AssertionError("extra login")):
            for _ in range(2):
                with self.assertRaises(InterruptedError):
                    gw.gateway_loop()
                self.assertEqual(gw.read_state()["state"], "needs_attention")
                self.assertEqual(gw.read_state()["attempts"], gw.BUDGET)
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            self.assertEqual(gw.main("reset"), 0)
        self.assertEqual(gw.read_state()["attempts"], 0)

    def test_three_failed_processes_stop_even_after_supervisor_restart(self):
        fixture = self.root / "fixtures"
        fixture.mkdir()
        def script(path, body):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o700)
        attempts = self.root / "attempts"
        script(self.root / "tools/ibgateway/1/jre/bin/java", "exit 1\n")
        script(self.root / "tools/ibc/1/scripts/ibcstart.sh", f"echo tried >> '{attempts}'\nexit 1\n")
        (self.root / "tools/ibc/1/IBC.jar").touch()
        script(self.root / "current/bin/finance-ib", "exit 1\n")
        script(fixture / "xdpyinfo", "exit 0\n")
        subprocess.run(["cc", "-shared", "-fPIC", REPO / "deploy/ibgateway/loopback.c",
                        "-ldl", "-o", gw.POLICY], check=True)
        saved_sleep = time.sleep
        backoffs = []
        def sleep(seconds):
            current = gw.read_state()["state"]
            if current == "needs_attention":
                raise InterruptedError()
            if current == "backoff":
                backoffs.append(seconds)
            saved_sleep(.01)
        environment = dict(PATH=str(fixture) + ":" + os.environ["PATH"], IB_GATEWAY_VERSION="1", IBC_VERSION="1")
        with patch.dict(os.environ, environment), patch.object(gw.time, "sleep", sleep), \
             patch.object(gw, "credentials", return_value={"IBKR_USERNAME": "fixture", "IBKR_PASSWORD": "fixture"}):
            for _ in range(2):
                with self.assertRaises(InterruptedError):
                    gw.startup()
        self.assertEqual(attempts.read_text().splitlines(), ["tried"] * 3)
        self.assertEqual(backoffs, [5, 20])
        self.assertFalse(gw.INI.exists())
        self.assertEqual(gw.read_state()["code"], "login_or_2fa")

    def test_offline_install_fails_on_corrupted_input_with_no_installed_files(self):
        cache = self.root / "cache/downloads/ibgateway"
        cache.mkdir(parents=True)
        (cache / "ibgateway-10.50.1g-standalone-linux-x64.sh").write_bytes(b"invalid")
        for mode in ("--offline", "--verify-only"):
            result = subprocess.run([REPO / "scripts/install-ibgateway.sh", mode],
                                    env=self.env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FAILED", result.stdout)
        self.assertEqual(list((self.root / "tools").iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
