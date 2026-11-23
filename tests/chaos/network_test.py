"""Scope, alert and netfilter cleanup safeguards for the Linux network drill."""
from datetime import datetime, timezone
import os
from pathlib import Path
import select
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import network


class NetworkDrillTests(unittest.TestCase):
    def test_local_fixture_cannot_reach_a_candidate_public_origin(self):
        flags = SimpleNamespace(protocol_fixture=True, paper_only=False, public_origin=None,
                                fixture_origin="https://www.viramoon.com")
        with self.assertRaisesRegex(RuntimeError, "127.0.0.1"):
            network.public_target(flags, flags.fixture_origin)
        flags.fixture_origin = "https://127.0.0.1:18443"
        self.assertEqual(network.public_target(flags, flags.fixture_origin), flags.fixture_origin)
        with self.assertRaises(RuntimeError):
            network.public_target(flags, "https://www.viramoon.com")
        flags.protocol_fixture, flags.fixture_origin = False, None
        flags.public_origin = "https://fertile.viramoon.com"
        with self.assertRaisesRegex(RuntimeError, "match"):
            network.public_target(flags, "https://www.viramoon.com")
        self.assertEqual(network.public_target(flags, flags.public_origin), flags.public_origin)
        flags.paper_only, flags.public_origin = True, None
        self.assertIsNone(network.public_target(flags, "https://fertile.viramoon.com"))

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
