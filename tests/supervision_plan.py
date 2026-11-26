"""Check the release/secret boundary without touching any persistent box files."""
import configparser
from contextlib import redirect_stdout
from email.message import Message
import importlib.util
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
import sys
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.request import AbstractHTTPHandler
from urllib.response import addinfourl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supervision as ctl
import supervisor_drill as drill
from runtime_root import resolve_root


PUBLIC_PROBE = "https://supervision.example.test/healthz/live"
LOCAL_PROBE = "http://127.0.0.1:8081/healthz/live"


def probe_response(url, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    response = addinfourl(BytesIO(), headers, url, 302 if location else 200)
    response.msg = "Found" if location else "OK"
    return response


class RuntimeRoot(unittest.TestCase):
    def test_shell_and_python_use_the_same_precedence(self):
        resolver = Path(__file__).resolve().parents[1] / "scripts/runtime-root.sh"
        cases = [
            ({}, "/home/box/fertile"),
            ({"FERTILE_PROFILE": "stable"}, "/var/lib/fertile"),
            ({"FERTILE_PROFILE": "stable", "FERTILE_BOX_HOME": "/tmp/fertile-box/"}, "/tmp/fertile-box/fertile"),
            ({"FERTILE_PROFILE": "stable", "FERTILE_BOX_HOME": "/tmp/fertile-box",
              "FERTILE_ROOT": "/tmp/fertile-root"}, "/tmp/fertile-root"),
        ]
        for env, expected in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                shell = subprocess.check_output(["/bin/bash", "-c", 'source "$1"; fertile_root', "bash", resolver],
                                                text=True).strip()
                self.assertEqual(shell, expected)
                self.assertEqual(resolve_root(), Path(expected))


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

    def assert_probe(self, transport, url, agent):
        self.assertEqual(transport.call_count, 1, "the probe must contact only the configured URL")
        request = transport.call_args.args[1]
        self.assertEqual(request.full_url, url)
        version = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text().strip()
        self.assertEqual(request.get_header("User-agent"), f"{agent}/{version}")

    def status_probe(self, url, responses):
        self.write(live=url)
        output = StringIO()
        with (patch.object(sys, "argv", ["fertile", "status", "--json"]), redirect_stdout(output),
              patch.object(ctl, "daemon_pid", return_value=0),
              patch.object(AbstractHTTPHandler, "do_open", side_effect=responses) as transport):
            ctl.main()
        return json.loads(output.getvalue())["services"]["test"]["live"], transport

    def test_status_public_probe_uses_versioned_user_agent(self):
        code, transport = self.status_probe(PUBLIC_PROBE, [probe_response(PUBLIC_PROBE)])
        self.assertEqual(code, 200)
        self.assert_probe(transport, PUBLIC_PROBE, "fertile-supervision")

    def test_status_reports_redirect_without_contacting_public_target(self):
        responses = [probe_response(LOCAL_PROBE, PUBLIC_PROBE), probe_response(PUBLIC_PROBE)]
        code, transport = self.status_probe(LOCAL_PROBE, responses)
        self.assertEqual(code, 302)
        self.assert_probe(transport, LOCAL_PROBE, "fertile-supervision")

    def test_drill_probe_rejects_redirect_without_contacting_public_target(self):
        api = {"live_url": LOCAL_PROBE, "live": 200, "pid": 12}
        responses = [probe_response(LOCAL_PROBE, PUBLIC_PROBE), probe_response(PUBLIC_PROBE)]
        with (patch.object(drill, "running_initial", return_value=(10, {"finance-api": api}, {})),
              patch.object(drill, "identity", side_effect=[10, 12]), patch.object(drill, "os"),
              patch.object(drill.signal, "pidfd_send_signal"), patch.object(drill, "report"),
              patch.object(drill, "wait_status", return_value={"services": {"finance-api": {"pid": 13}}}),
              patch.object(AbstractHTTPHandler, "do_open", side_effect=responses) as transport):
            with self.assertRaisesRegex(RuntimeError, "independent live GET returned HTTP 302"):
                drill.chaos()
        self.assert_probe(transport, LOCAL_PROBE, "fertile-supervisor-drill")

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

    def test_paper_credential_probe_uses_effective_root(self):
        self.write(ib=True)
        with (patch.dict(os.environ, {"FERTILE_ROOT": str(self.root), "FERTILE_MARKET_SOURCE": "ib"}),
              patch.object(ctl, "check_secret", return_value="needs_credentials") as check):
            plan = ctl.build_plan()
        self.assertEqual(plan["services"]["test"]["skip"], "needs_credentials")
        check.assert_called_once_with(self.root / "secrets/ibkr-paper.env")

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

    def test_rendered_launcher_runs_without_version_in_runtime_directory(self):
        self.write(command=["/bin/echo", "launcher reached fixture"])
        ctl.render(ctl.build_plan())
        config = configparser.RawConfigParser()
        config.read(self.run / "programs.conf")
        command = shlex.split(config["program:test"]["command"])
        self.assertFalse((self.run / "VERSION").exists())
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=5,
                                env={**os.environ, "FERTILE_ROOT": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "launcher reached fixture\n")

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

    def test_controller_routes_a_legacy_release_to_restored_local_targets(self):
        release = self.root / "releases" / ("a" * 40)
        deployment = release / "fertile/deploy"
        deployment.mkdir(parents=True)
        (release / "fertile/scripts").mkdir()
        (release / "fertile/scripts/prepare-web-db.sql").touch()
        (self.root / "current").symlink_to(release)
        (deployment / "services.yaml").write_text(json.dumps({"services": [
            {"name": "postgres", "command": ["${release}/fertile/scripts/postgres.sh", "foreground"]},
            {"name": "web", "command": ["${release}/bin/web"]},
            {"name": "healthwatch", "command": ["${release}/fertile/scripts/healthwatch"]},
        ]}))
        data = self.root / "restored"
        env = {"PATH": os.defpath, "FERTILE_DATA_ROOT": str(data), "FERTILE_WEB_DATABASE": "web_rollback",
               "FERTILE_RESEARCH_ROOT": str(data / "research")}
        with patch.dict(os.environ, env, clear=True), patch.object(ctl.shutil, "which", return_value="/bin/true"):
            plan = ctl.build_plan()
        pg, web, healthwatch = (plan["services"][name] for name in ("postgres", "web", "healthwatch"))
        self.assertEqual(pg["command"], [str(ctl.SCRIPTS / "postgres.sh"), "foreground"])
        self.assertEqual(pg["env"]["FERTILE_WEB_PEER"], "1")
        self.assertEqual(web["command"], [str(release / "bin/web")])
        self.assertIn("fertile_web@localhost/web_rollback?", web["env"]["DATABASE_URL"])
        self.assertEqual(healthwatch["command"], [str(ctl.SCRIPTS / "healthwatch")])
        self.assertEqual(healthwatch["env"]["FERTILE_RESEARCH_ROOT"], str(data / "research"))
        self.assertEqual(healthwatch["env"]["PGDATABASE"], "web_rollback")
        with (patch.object(ctl.subprocess, "run", return_value=Mock(returncode=0)) as run,
              patch.object(ctl.subprocess, "check_output", side_effect=["1\n", "f\n"])):
            ctl.prepare_web(plan)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("web_database=web_rollback", commands[1])
        self.assertEqual(commands[1][-1], ctl.SCRIPTS / "prepare-web-db.sql")
        self.assertIn('REVOKE CREATE ON DATABASE "web_rollback" FROM fertile_web', commands[2])
        self.assertEqual(commands[3], [release / "bin/web", "migrate"])

    def test_switch_only_checks_the_release_and_keeps_services_stopped(self):
        active, target = (self.root / "releases" / (char * 40) for char in ("a", "b"))
        active.mkdir(parents=True)
        (self.root / "current").symlink_to(active)
        for item in ("fertile/scripts/fertile", "fertile/deploy/services.yaml", "bin/web", "bin/finance-api"):
            file = target / item
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch()
        components = dict.fromkeys(("finance", "web", "frontend"), "c" * 40)
        (target / "manifest.json").write_text(json.dumps({"components": components}))
        with (patch.object(ctl, "daemon_pid", return_value=0), patch.object(ctl, "ensure") as ensure,
              patch.object(ctl, "build_plan", return_value={"default": True, "data": str(self.root / "restored")}),
              patch.object(ctl.subprocess, "run") as verify):
            ctl.rollback(target.name, switch_only=True)
        self.assertEqual((self.root / "current").resolve(), target)
        self.assertEqual(verify.call_args.args[0][2:5], ["verify", target, target.name])
        ensure.assert_not_called()

        with patch.object(ctl, "daemon_pid", return_value=42):
            with self.assertRaisesRegex(RuntimeError, "stop supervisord"):
                ctl.rollback(active.name, switch_only=True)
        self.assertEqual((self.root / "current").resolve(), target)

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
