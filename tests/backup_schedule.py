"""Test the healthwatch backup schedule without starting the sampler or any child."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup_remote
import healthwatch


class RemoteSchedule(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for directory in ("config", "run", "logs/healthwatch"):
            (self.root / directory).mkdir(parents=True)
        self.name = "fixture:backup"
        self.config = self.root / "config/backup.conf"
        self.config.write_text(f"FERTILE_BACKUP_REMOTE={self.name}\n")
        self.instant = datetime(2026, 11, 27, 3, 0, tzinfo=timezone.utc)
        self.settings = {"backup_at_utc": "03:00"}
        self.scheduler = healthwatch.RemoteBackup(self.root)
        self.children = []
        env = patch.dict(os.environ, {"FERTILE_ROOT": "/tmp/unrelated-fixture"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        launch = patch.object(healthwatch.subprocess, "Popen", side_effect=self.child)
        self.launch = launch.start()
        self.addCleanup(launch.stop)

    def child(self, *_args, **_kwargs):
        child = Mock()
        child.poll.return_value = None
        self.children.append(child)
        return child

    def set_state(self, **keys):
        state = {"remote_id": backup_remote.reference_id(self.name)}
        for key, value in keys.items():
            state[key] = value.strftime(backup_remote.STAMP) if isinstance(value, datetime) else value
        backup_remote.state_path(self.root).write_text(json.dumps(state))

    def observe(self, instant=None, *, once=False):
        return self.scheduler.observe(self.settings, instant or self.instant, once)

    def commands(self):
        return [call.args[0][-1] for call in self.launch.call_args_list]

    def test_hourly_does_not_wait_for_running_full(self):
        before_full = self.instant - timedelta(hours=1)
        self.set_state(full=before_full, hourly=self.instant - timedelta(minutes=1), last_point=before_full)
        checks = self.observe(self.instant - timedelta(seconds=1))
        self.assertEqual(checks["backup:remote_full"]["state"], "ok")
        self.launch.assert_not_called()

        self.observe()
        self.assertEqual(self.commands(), ["full", "hourly"])
        self.assertEqual(self.launch.call_args.kwargs["env"]["FERTILE_ROOT"], str(self.root))
        self.assertEqual(self.launch.call_args.args[0][1], healthwatch.SCRIPTS / "backup_remote.py")
        checks = self.observe(self.instant + timedelta(minutes=1))
        self.assertEqual(checks["backup:remote_full"]["detail"], "encrypted backup running")
        self.assertEqual(checks["backup:remote_hourly"]["detail"], "encrypted backup running")
        self.assertEqual(self.commands(), ["full", "hourly"])

    def test_retry_cooldown_is_durable_and_per_job(self):
        self.observe()
        self.set_state(hourly=self.instant, last_point=self.instant)
        self.children[0].poll.return_value = 1
        self.children[1].poll.return_value = 0
        self.assertEqual(self.observe(self.instant + timedelta(minutes=1))["backup:remote_full"]["state"], "failed")
        self.scheduler = healthwatch.RemoteBackup(self.root)
        checks = self.observe(self.instant + timedelta(minutes=14))
        self.assertEqual(checks["backup:remote_full"]["state"], "failed")
        self.assertEqual(checks["backup:remote_hourly"]["state"], "ok")
        self.assertEqual(self.commands(), ["full", "hourly"])
        self.observe(self.instant + timedelta(minutes=15))
        self.assertEqual(self.commands(), ["full", "hourly", "full"])

    def test_missed_hour_catches_up_and_old_full_counts_for_its_own_day(self):
        last_full = self.instant - timedelta(days=1)
        self.set_state(full=last_full, hourly=last_full, last_point=last_full)
        self.observe(self.instant - timedelta(minutes=10))
        self.assertEqual(self.commands(), ["hourly"])
        self.children[0].poll.return_value = 0
        self.set_state(full=last_full, hourly=self.instant - timedelta(minutes=10), last_point=self.instant - timedelta(minutes=10))
        self.observe(self.instant)
        self.assertEqual(self.commands(), ["hourly", "full", "hourly"])

    def test_rpo_bound_and_degradation_use_cached_recoverable_point(self):
        first = self.observe(once=True)["backup:remote_age"]
        self.assertTrue(first["alert"])
        self.assertIsNone(first["last_recoverable_at"])
        previous = self.instant - timedelta(hours=1)
        self.set_state(full=previous, last_point=previous)
        fresh = self.observe(once=True)["backup:remote_age"]
        self.assertEqual(fresh["state"], "ok")
        self.assertEqual(fresh["age_seconds"], 3600)
        self.assertEqual(fresh["last_recoverable_at"], "2026-11-27T02:00:00Z")
        stale = self.observe(self.instant + timedelta(seconds=1), once=True)["backup:remote_age"]
        self.assertEqual(stale["state"], "failed")
        self.assertEqual(stale["age_seconds"], 3601)
        self.set_state(full=self.instant, last_point=self.instant, degraded=True)
        degraded = self.observe(once=True)["backup:remote_age"]
        self.assertTrue(degraded["alert"])
        self.assertTrue(degraded["degraded"])
        self.launch.assert_not_called()

    def test_disabled_or_one_shot_does_not_launch_uploads(self):
        checks = self.observe(once=True)
        self.assertEqual(checks["backup:remote_full"]["state"], "skipped")
        self.assertEqual(checks["backup:remote_hourly"]["state"], "skipped")
        self.settings["backup_at_utc"] = None
        checks = self.observe()
        self.assertEqual(checks["backup:remote_full"]["state"], "skipped")
        self.assertEqual(checks["backup:remote_hourly"]["state"], "skipped")
        self.config.write_text("FERTILE_BACKUP_REMOTE=\n")
        self.assertEqual(self.observe()["backup:remote_age"]["state"], "skipped")
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
