"""Check the release/secret boundary without touching any persistent box files."""
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import sqlite3
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supervision as ctl


class RuntimePlan(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = self.root / "run"
        self.logs = self.root / "logs"
        for directory in [self.run, self.logs, self.root / "config"]:
            directory.mkdir()
        for key, value in (("ROOT", self.root), ("RUN", self.run), ("LOGS", self.logs),
                           ("CONF", self.run / "supervisord.conf"), ("SNAPSHOT", self.run / "services.json")):
            patcher = patch.object(ctl, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manifest = self.root / "services.yaml"
        env = patch.dict(os.environ, {"FERTILE_SERVICE_MANIFEST": str(self.manifest), "PATH": os.defpath}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def write(self, **extras):
        svc = {"name": "test", "directory": str(self.root), "command": ["/bin/true"], **extras}
        self.manifest.write_text(json.dumps({"services": [svc]}))

    def test_secret_referenced_by_name_and_validated_at_use(self):
        self.write(env_file="test.env")
        self.assertEqual(ctl.build_plan()["services"]["test"]["skip"], "needs_credentials")
        path = self.root / "secrets/test.env"
        path.parent.mkdir()
        path.write_text("EXAMPLE_VALUE=must-stay-out-of-rendered-files\n")
        path.chmod(0o644)
        self.assertEqual(ctl.build_plan()["services"]["test"]["skip"], "invalid_credentials_permissions")
        path.chmod(0o600)
        plan = ctl.build_plan()
        self.assertFalse(plan["services"]["test"]["skip"])
        ctl.render(plan)
        self.assertFalse(any("must-stay-out-of-rendered-files" in p.read_text()
                             for p in self.run.rglob("*") if p.is_file()))
        path.rename(path.with_suffix(".old"))
        path.symlink_to(path.with_suffix(".old"))
        self.assertEqual(ctl.build_plan()["services"]["test"]["skip"], "invalid_credentials_permissions")
        with self.assertRaises(OSError):
            ctl.read_env(path, secret=True)
        self.write(env_file="ibkr-paper.env")
        with self.assertRaisesRegex(ValueError, "invalid service env_file"):
            ctl.build_plan()

    def test_restore_recovery_prevents_all_service_starts_and_switches(self):
        (self.run / "restore-journal.json").write_text("{}")
        for args in (["up"], ["ensure"], ["ensure-up"], ["restart", "test"], ["rollback", "1234567"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["fertile", *args]):
                with self.assertRaisesRegex(RuntimeError, "unfinished restore"):
                    ctl.main()
        with patch.object(sys, "argv", ["fertile", "down"]), patch.object(ctl, "daemon_pid", return_value=0):
            ctl.main()

    def test_profile_mode_and_deterministic_render(self):
        self.write()
        config = self.root / "config/runtime.env"
        config.write_text("FERTILE_PROFILE=stable\nFERTILE_MARKET_SOURCE=replay\n")
        plan = ctl.build_plan()
        self.assertEqual(plan["data"], str(self.root / "data/replay"))
        env = plan["services"]["test"]["env"]
        self.assertEqual((env["FERTILE_PROFILE"], env["PYTHONDONTWRITEBYTECODE"]), ("stable", "1"))
        ctl.render(plan)
        stamp = ctl.CONF.stat().st_mtime_ns
        ctl.render(ctl.build_plan())
        self.assertEqual(stamp, ctl.CONF.stat().st_mtime_ns)
        with patch.dict(os.environ, {"FERTILE_MARKET_SOURCE":"ib"}):
            self.assertEqual(ctl.build_plan()["data"], str(self.root / "data"))
        config.write_text("FERTILE_MARKET_SOURCE=live\n")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ctl.build_plan()

    def test_data_root_command_matches_service_plan_and_honors_overrides(self):
        self.write()
        config = self.root / "config/runtime.env"
        paper = self.root / "data-paper"
        override = self.root / "data-override"
        cases = (("", {}, self.root / "data"),
                 ("FERTILE_MARKET_SOURCE=replay\n", {}, self.root / "data/replay"),
                 (f'FERTILE_DATA_ROOT="{paper}"\n', {}, paper),
                 (f"FERTILE_DATA_ROOT={paper}\n", {"FERTILE_DATA_ROOT": str(override)}, override))
        for contents, env, expected in cases:
            with self.subTest(expected=expected), patch.dict(os.environ, env):
                config.write_text(contents)
                output = StringIO()
                with patch.object(sys, "argv", ["fertile", "data-root"]), redirect_stdout(output):
                    ctl.main()
                self.assertEqual(output.getvalue(), f"{expected}\n")
                self.assertEqual(ctl.build_plan()["data"], str(expected))

    def test_8080_reserved_and_research_paths_are_owned(self):
        self.write(name="web")
        self.assertEqual(ctl.build_plan()["services"]["web"]["env"]["WEB_PORT"], "8090")
        for key in ("WEB_PORT", "FINANCE_API_PORT", "FINANCE_COLLECTOR_PORT"):
            with patch.dict(os.environ, {key: "8080"}), self.assertRaisesRegex(ValueError, "8080 belongs"):
                ctl.build_plan()
        with patch.dict(os.environ, {"FERTILE_RESEARCH_ROOT": "/home/box/quant-research"}):
            with self.assertRaisesRegex(ValueError, "fertile data/research"):
                ctl.build_plan()

    def test_research_root_accepts_default_and_concrete_children(self):
        self.write()
        base = self.root / "data/research"
        self.assertEqual(ctl.build_plan()["services"]["test"]["env"]["FERTILE_RESEARCH_ROOT"], str(base))
        for path in (base, base / "catalog"):
            with self.subTest(path=path), patch.dict(os.environ, FERTILE_RESEARCH_ROOT=str(path)):
                env = ctl.build_plan()["services"]["test"]["env"]
                self.assertEqual(env["FERTILE_RESEARCH_ROOT"], str(path))

    def test_research_root_rejects_relative_paths_and_escapes(self):
        self.write()
        base = self.root / "data/research"
        base.mkdir(parents=True)
        outside = self.root / "data/other"
        outside.mkdir()
        link = base / "escape"
        link.symlink_to(outside, target_is_directory=True)
        for path in (Path("data/research"), outside, base / "../other", base / "catalog/../other", link):
            with self.subTest(path=path), patch.dict(os.environ, FERTILE_RESEARCH_ROOT=str(path)):
                with self.assertRaisesRegex(ValueError, "fertile data/research"):
                    ctl.build_plan()

    def test_database_audit_rebases_inherited_data_roots(self):
        spec = importlib.util.spec_from_file_location("web_database", Path(__file__).with_name("web-database.py"))
        database = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(database)
        caller_env = {
            "PATH": os.defpath,
            "FERTILE_BOX_HOME": str(self.root),
            "FERTILE_ROOT": str(self.root),
            "FERTILE_DATA_ROOT": str(self.root / "data/replay"),
            "FERTILE_RESEARCH_ROOT": str(self.root / "data/research"),
            "WEB_DATABASE_TEST_BIN": "/bin/true",
            "WEB_DATABASE_TEST_CONFIG": str(self.root),
        }

        class ReachedDatabaseInit(Exception):
            pass

        with (patch.dict(os.environ, caller_env, clear=True),
              patch.object(ctl, "ROOT", self.root), patch.object(ctl, "RUN", self.run),
              patch.object(ctl, "check_secret", return_value="needs_credentials"),
              patch.object(ctl.shutil, "which", return_value="/bin/true"),
              patch.object(database, "run", side_effect=ReachedDatabaseInit) as run):
            with self.assertRaises(ReachedDatabaseInit):
                database.audit()

        self.assertEqual(run.call_args.args[-1], "init")
        env = run.call_args.kwargs["env"]
        audit_root = Path(env["FERTILE_ROOT"])
        self.assertNotEqual(audit_root, self.root)
        self.assertEqual(env["FERTILE_DATA_ROOT"], str(audit_root / "data/replay"))
        self.assertEqual(env["FERTILE_RESEARCH_ROOT"], str(audit_root / "data/research"))

    def test_external_group_keeps_legacy_supervisor_untouched(self):
        self.write(name="cloudflared")
        with self.assertRaisesRegex(ValueError, "external to the fertile supervisor"):
            ctl.build_plan()
        self.write()
        api = Mock()
        api.getAllProcessInfo.return_value = [{"name": "edge", "group": "tunnel", "pid": 42, "statename": "RUNNING"}]
        with patch.object(ctl, "connection", return_value=api), patch.object(ctl, "daemon_pid", return_value=1):
            for task in (ctl.ensure, ctl.down):
                with self.assertRaisesRegex(RuntimeError, "Ruler must resolve ownership"):
                    task()
        api.stopProcess.assert_not_called()
        api.reloadConfig.assert_not_called()
        api.shutdown.assert_not_called()

    def test_legacy_bundle_rejects_a_research_queue_without_mutating_it(self):
        plan = {"data": str(self.root / "data"), "release": str(self.root / "old"), "default": True}
        queue = self.root / "data/finance-state/backtests/jobs.sqlite3"
        queue.parent.mkdir(parents=True)
        with sqlite3.connect(queue) as db:
            db.execute("CREATE TABLE jobs (request TEXT)")
            db.execute("INSERT INTO jobs VALUES (?)", ('{"strategy":"DemoStrategy"}',))
        ctl.check_queue_compatibility(plan)
        with sqlite3.connect(queue) as db:
            db.execute("INSERT INTO jobs VALUES (?)", ('{"strategy":"SmaCrossover"}',))
        before = queue.read_bytes()
        with self.assertRaisesRegex(ValueError, "cannot read the existing research queue"):
            ctl.check_queue_compatibility(plan)
        self.assertEqual(queue.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
