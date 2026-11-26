"""Scope, alert and netfilter cleanup safeguards for the Linux network drill."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import call, patch
from types import SimpleNamespace

import network


def flags(**options):
    values = dict(protocol_fixture=False, paper_only=False, public_origin=None, fixture_origin=None,
                  duration=120, session_open=None, session_close=None)
    values.update(options)
    return SimpleNamespace(**values)


def plan_for(env):
    return {"services": {"healthwatch": {"env": env}}}


class NetworkDrillTests(unittest.TestCase):
    def test_local_fixture_cannot_reach_a_candidate_public_origin(self):
        args = flags(protocol_fixture=True, fixture_origin="https://outside.invalid")
        plan = plan_for({"FERTILE_PUBLIC_ORIGIN": args.fixture_origin})
        with self.assertRaisesRegex(RuntimeError, "127.0.0.1"):
            network.public_target(args, plan)
        args.fixture_origin = "https://127.0.0.1:18443"
        with self.assertRaisesRegex(RuntimeError, "must agree"):
            network.public_target(args, plan)
        self.assertEqual(network.public_target(args, plan_for({"FERTILE_PUBLIC_ORIGIN": args.fixture_origin})),
                         args.fixture_origin)
        args.public_origin = "https://web.example.test"
        with self.assertRaisesRegex(RuntimeError, "protocol fixture must never use a public domain"):
            network.public_target(args, plan)

    def test_public_check_requires_cli_opt_in_and_matching_healthwatch_origin(self):
        origin = "https://web.example.test"
        with TemporaryDirectory() as folder:
            path = Path(folder) / "healthwatch.yaml"
            plan = plan_for({"FERTILE_HEALTH_CONFIG": str(path)})
            self.assertIsNone(network.public_target(flags(), plan))  # No overlay reads without opt-in.
            path.write_text("public_origin: https://WEB.example.test:443/\n")
            self.assertIsNone(network.public_target(flags(paper_only=True), plan))
            self.assertEqual(network.public_target(flags(public_origin=origin), plan), origin)
            plan["services"]["healthwatch"]["env"]["FERTILE_PUBLIC_ORIGIN"] = "https://other.invalid"
            with self.assertRaisesRegex(RuntimeError, "must match"):
                network.public_target(flags(public_origin=origin), plan)
            self.assertIsNone(network.public_target(flags(), plan))
        with self.assertRaisesRegex(RuntimeError, "match"):
            network.public_target(flags(public_origin=origin), plan_for({}))
        with self.assertRaisesRegex(RuntimeError, "paper-only mode does not take a public origin"):
            network.public_target(flags(paper_only=True, public_origin=origin), plan_for({"FERTILE_PUBLIC_ORIGIN": origin}))

    def test_shell_rejects_invalid_public_origin_before_opening_the_drill_lock(self):
        with TemporaryDirectory() as folder:
            env = dict(os.environ, FERTILE_ROOT=folder, FERTILE_CONTROL_PYTHON=sys.executable)
            for value in ("http://web.example.test", "https://user@web.example.test",
                          "https://web.example.test/path", "https://web.example.test:65536"):
                with self.subTest(value=value):
                    proc = subprocess.run([network.HERE / "network.sh", "--public-origin", value],
                                          env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn("--public-origin", proc.stderr)
                    self.assertIn("HTTPS origin", proc.stderr)

    def test_network_baseline_probes_public_web_only_with_explicit_opt_in(self):
        class BaselineComplete(RuntimeError):
            pass

        origin = "https://web.example.test"
        with TemporaryDirectory() as folder:
            root = Path(folder)
            release = root / "release"
            release.mkdir()
            (root / "current").symlink_to(release)
            (root / "run").mkdir()
            alerts = root / "logs/alerts"
            alerts.mkdir(parents=True)
            (alerts / "alerts.jsonl").touch()
            config = root / "collector.yaml"
            config.write_text("source: ib\nbar_spec: 1-MINUTE-LAST\ncatalog: catalog\ninstruments: [T.SIM]\n"
                              "max_age_seconds: 120\n")
            for relative in ("catalog/.finance-collector/source.json", "finance-state/collector/catalog.json"):
                path = root / relative
                path.parent.mkdir(parents=True)
                path.write_text('{"source":"ib"}')
            plan = plan_for({"FERTILE_PUBLIC_ORIGIN": origin})
            plan["release"] = str(release)
            plan["services"]["finance-collector"] = {"env": {"FERTILE_DATA_ROOT": str(root),
                                                                 "FINANCE_COLLECTOR_CONFIG": str(config)}}
            (root / "run/services.json").write_text(json.dumps(plan))
            active = dict.fromkeys(("healthwatch", "postgres", "finance-backtest-worker", "ib-gateway"), {})
            for service, port in (("finance-api", 8081), ("finance-collector", 8082), ("web", 8090)):
                active[service] = {"ready_url": f"http://127.0.0.1:{port}/healthz/ready", "ready": 200}
            minute = time.time_ns() // network.NS_MINUTE * network.NS_MINUTE
            with (patch.object(network, "ROOT", root), patch.object(network, "session", return_value=None),
                  patch.object(network.drill, "running_initial", return_value=(1, active, {})),
                  patch.object(network.healthwatch, "webhook_url", return_value=None),
                  patch.object(network, "marks", return_value={"T.SIM": minute}),
                  patch.object(network, "local_market", return_value={"T.SIM"}),
                  patch.object(network, "catalog_window"), patch.object(network, "paper_probe", side_effect=BaselineComplete),
                  patch.object(network.healthwatch, "public_ready", return_value={"state": "ok"}) as ready,
                  patch.object(network.healthwatch, "http_json", return_value=(200, {"web": {"started_at": "now"}})) as http):
                with self.assertRaises(BaselineComplete):
                    network.run(flags())
                ready.assert_not_called()
                http.assert_not_called()
                with self.assertRaises(BaselineComplete):
                    network.run(flags(public_origin=origin))
                ready.assert_called_once_with(origin, 4)
                self.assertEqual(http.call_args_list, [call("http://127.0.0.1:8090/api/v1/system/status", 3, local=True),
                                                      call(origin + "/api/v1/system/status", 5)])

    def test_early_close_and_weekend_do_not_count_as_trading(self):
        class Clock(datetime):
            @classmethod
            def now(cls, _tz=None):
                return cls(2026, 11, 27, 17, 55, tzinfo=timezone.utc)

        with patch.object(network, "datetime", Clock), patch.object(network.subprocess, "check_output") as calendar:
            calendar.return_value = b'["2026-11-27T14:30:00+00:00","2026-11-27T18:00:00+00:00"]'
            with self.assertRaisesRegex(RuntimeError, "closing time"):
                network.session(Path("/release"), None, None, 120)
            with self.assertRaisesRegex(RuntimeError, "including early closes"):
                network.session(Path("/release"), "2026-11-27T09:30:00-05:00", "2026-11-27T16:00:00-05:00", 120)
            calendar.return_value = b'null'
            with self.assertRaisesRegex(RuntimeError, "exchange is closed"):
                network.session(Path("/release"), None, None, 120)

    def test_prior_or_out_of_order_alerts_do_not_complete_this_fault(self):
        def event(which, when):
            return dict(event=which, timestamp=datetime.fromtimestamp(when, timezone.utc).isoformat())
        key = "market:fresh"
        alerts = {key: [event("failure", 2), event("recovery", 18)]}
        self.assertFalse(network.completed(alerts, key, 10, 20))
        alerts[key] = [event("recovery", 12), event("failure", 18)]
        self.assertFalse(network.completed(alerts, key, 10, 20))
        alerts[key].append(event("recovery", 19))
        self.assertFalse(network.completed(alerts, key, 10, 20))
        alerts[key].append(event("recovery", 25))
        self.assertTrue(network.completed(alerts, key, 10, 20))

    def test_coordinator_kill_clears_only_the_scoped_rule(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            rule = root / "rule"
            fake = root / "iptables"
            fake.write_text(
                '#!/bin/bash\n'
                'if [[ $3 == -S ]]; then exit 0; fi\n'
                '[[ "$*" == *" -o lo -p tcp -d 127.0.0.1 --dport 4002 -m comment --comment fertile-drill-"* ]] || exit 42\n'
                'case "$3" in -I) touch "$QA_RULE";; -C) test -f "$QA_RULE";; -D) rm "$QA_RULE";; *) exit 42;; esac\n'
            )
            fake.chmod(0o700)
            env = {**os.environ, "PATH": folder + os.pathsep + os.environ["PATH"], "QA_RULE": str(rule)}
            controller = subprocess.Popen([sys.executable, __file__, "--hold"], env=env,
                                          stdout=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([controller.stdout], [], [], 5)[0])
                self.assertEqual(controller.stdout.readline().strip(), "active")
                self.assertTrue(rule.exists())
                controller.kill()
                controller.wait(5)
                limit = time.monotonic() + 5
                while rule.exists():
                    self.assertLess(time.monotonic(), limit, "orphan guardian failed to clear its rule")
                    time.sleep(.02)
            finally:
                controller.kill()
                controller.wait()
                controller.stdout.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--hold"]:
        with network.blocked_port(120):
            print("active", flush=True)
            time.sleep(30)
    else:
        unittest.main(verbosity=2)
