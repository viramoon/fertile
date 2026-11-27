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
import shutil
import subprocess
from tempfile import TemporaryDirectory
import sys
import sqlite3
import unittest
from unittest.mock import Mock, call, patch
from urllib.request import AbstractHTTPHandler
from urllib.response import addinfourl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supervision as ctl
import service_releases
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


class FinanceReleaseArtifacts(unittest.TestCase):
    def test_optional_finance_binaries_come_from_selected_cargo_targets(self):
        manifest = Path(__file__).resolve().parents[1] / "scripts/release-manifest.py"
        with TemporaryDirectory() as directory:
            source = Path(directory) / "cargo-metadata.json"
            for declared, expected in (
                (("finance-binance", "finance-market-wal", "finance-strategy"),
                 ["finance-binance", "finance-market-wal", "finance-strategy"]),
                (("finance-market-wal",), ["finance-market-wal"]),
            ):
                source.write_text(json.dumps({"packages": [{"targets": [
                    *({"name": name, "kind": ["bin"]} for name in declared),
                    {"name": "finance-api", "kind": ["bin"]},
                    {"name": "finance-decision", "kind": ["bin"], "required-features": ["extra"]},
                ]}]}))
                result = subprocess.check_output(
                    [sys.executable, manifest, "optional-bins", "finance", source], text=True,
                )
                self.assertEqual(result.splitlines(), expected)


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

    def releases(self):
        production = ctl.SCRIPTS.parent
        newest, previous = (self.root / "releases" / (commit * 40) for commit in ("a", "b"))
        for release in (newest, previous):
            (release / "fertile/scripts").mkdir(parents=True)
            (release / "fertile/deploy").mkdir()
            shutil.copytree(production / "deploy/supervisor", release / "fertile/deploy/supervisor")
            shutil.copy(production / "scripts/supervision.py", release / "fertile/scripts/supervision.py")
            shutil.copy(production / "deploy/services.yaml", release / "fertile/deploy/services.yaml")
            (release / "fertile/scripts/prepare-web-db.sql").touch()
            (release / "finance/config/collector").mkdir(parents=True)
            (release / "finance/config/collector/cloudbox.yaml").write_text("source: replay\n")
            (release / "web").mkdir()
        (self.root / "current").symlink_to(newest)
        return newest, previous

    def rendered_programs(self, plan):
        ctl.render(plan)
        parser = configparser.RawConfigParser()
        parser.read(self.run / "programs.conf")
        return parser

    def test_worker_uses_the_indicator_binary_from_its_selected_release(self):
        base, previous = self.releases()
        with (patch.dict(os.environ, {"PATH": os.defpath, "FERTILE_MARKET_SOURCE": "replay"}, clear=True),
              patch.object(ctl.shutil, "which", return_value="/bin/true")):
            for release in (base, previous):
                if release == previous:
                    service_releases.select(self.root, base, "finance-backtest-worker", previous)
                services = ctl.build_plan()["services"]
                worker = services["finance-backtest-worker"]
                self.assertFalse(worker["skip"])
                self.assertEqual(worker["env"]["FINANCE_STRATEGY_BIN"], str(release / "bin/finance-strategy"))
                self.assertEqual(worker["command"], [str(release / "finance/scripts/finance-backtest-worker")])
                self.assertEqual(services["finance-backtest-worker-f13"]["skip"], "disabled")
                self.assertEqual(services["finance-live-decision"]["skip"], "disabled")
                self.assertEqual(services["finance-collector-binance"]["skip"], "disabled")

    def test_pinned_web_uses_selected_payload_without_reconfiguring_collector(self):
        base, previous = self.releases()
        old = ctl.manifest_rows(previous / "fertile/deploy/services.yaml")
        old["web"] = {key: old["web"][key] for key in ("name", "directory", "command", "live", "ready")}
        (previous / "fertile/deploy/services.yaml").write_text(json.dumps({"services": list(old.values())}))
        with (patch.dict(os.environ, {"PATH": os.defpath, "FERTILE_MARKET_SOURCE": "replay"}, clear=True),
              patch.object(ctl.shutil, "which", return_value="/bin/true")):
            first = ctl.build_plan()
            old_programs = self.rendered_programs(first)
            service_releases.select(self.root, base, "web", previous)
            second = ctl.build_plan()
            new_programs = self.rendered_programs(second)
            with patch.object(ctl.subprocess, "run") as run:
                ctl.prepare_web(second)
            run.assert_not_called()
        web = second["services"]["web"]
        self.assertEqual(web["release"], str(previous))
        self.assertEqual(web["command"][0], str(previous / "bin/web"))
        self.assertEqual(web["role"], "reader")
        self.assertEqual(web["env"]["WEB_AUTO_MIGRATE"], "false")
        self.assertEqual(web["env"]["WEB_STATIC_ROOT"], str(previous / "static"))
        self.assertTrue(web["pinned"])
        self.assertEqual(set(old_programs.sections()), set(new_programs.sections()))
        self.assertNotEqual(dict(old_programs["program:web"]), dict(new_programs["program:web"]))
        for section in set(old_programs.sections()) - {"program:web"}:
            self.assertEqual(dict(old_programs[section]), dict(new_programs[section]))

    def test_projector_and_notifier_capabilities_are_opt_in(self):
        release, _ = self.releases()
        secrets = self.root / "secrets"
        secrets.mkdir()
        config = secrets / "web-notifier.env"
        config.write_text("WEB_SMTP_HOST=relay.example.invalid\nWEB_SMTP_FROM=notice@example.invalid\n"
                          "WEB_SMTP_TO=owner@example.invalid\n")
        config.chmod(0o600)
        with (patch.dict(os.environ, {"PATH": os.defpath, "FERTILE_MARKET_SOURCE": "replay"}, clear=True),
              patch.object(ctl.shutil, "which", return_value="/bin/true")):
            defaults = ctl.build_plan()
            services = defaults["services"]
            self.assertEqual(services["finance-collector"]["source"], "replay")
            for name in ("finance-projector", "web-notifier", "finance-collector-binance",
                         "finance-collector-hyperliquid", "finance-live-decision", "finance-backtest-worker-f13"):
                self.assertEqual(services[name]["skip"], "disabled")
            enabled = ctl.build_plan(settings_overrides={"FERTILE_ENABLE_FINANCE_PROJECTOR": "1",
                                                         "FERTILE_ENABLE_WEB_NOTIFIER": "1"})
            programs = self.rendered_programs(enabled)
            projector = enabled["services"]["finance-projector"]
            notifier = enabled["services"]["web-notifier"]
            self.assertEqual(projector["command"], [
                str(release / "runtime/venv/bin/python"), "-m", "fertile_finance.market_projector",
                "--data-root", str(self.root / "data/replay"), "--wal-bin", str(release / "bin/finance-market-wal"),
            ])
            self.assertEqual(projector["env"]["PYTHONPATH"], str(release / "finance/python"))
            self.assertEqual(projector["volumes"][str(self.root / "data/replay/finance-state/market-wal")], "ro")
            self.assertEqual(notifier["env"]["DATABASE_URL"], enabled["services"]["web"]["env"]["DATABASE_URL"])
            self.assertEqual(programs["program:web-notifier"]["stopwaitsecs"], "75")
            self.assertFalse(notifier["skip"])
            with patch.object(ctl.subprocess, "run", return_value=Mock(returncode=0, stdout="t\n")) as psql:
                ctl.prepare_notifier(enabled)
                self.assertEqual(psql.call_args.kwargs["env"]["PGDATABASE"], notifier["env"]["PGDATABASE"])
                self.assertEqual(psql.call_args.kwargs["env"]["PGUSER"], "fertile_web")
                self.assertIn("replaces_id", psql.call_args.args[0][-1])
                psql.return_value.stdout = "f\n"
                with self.assertRaisesRegex(RuntimeError, "W3 database migrations"):
                    ctl.prepare_notifier(enabled)
        self.assertFalse(any(b"relay.example.invalid" in path.read_bytes()
                             for path in self.run.rglob("*") if path.is_file()))

    def test_container_uses_tini_and_a_foreground_supervisor(self):
        self.write()
        argv = ["/fixture/tini", "-s", "--", sys.executable, str(ctl.SCRIPTS / "supervision.py"), "_container"]
        with patch.object(ctl.shutil, "which", return_value="/fixture/tini"):
            self.assertEqual(ctl.container_command(), argv)
        with patch.object(ctl.shutil, "which", return_value=None), self.assertRaisesRegex(ValueError, "install tini"):
            ctl.container_command()
        process = Mock()
        process.wait.return_value = 24
        process.poll.return_value = 24
        with (patch.object(ctl, "daemon_pid", return_value=0), patch.object(ctl, "wait_daemon") as ready,
              patch.object(ctl, "reconcile") as reconcile, patch.object(ctl.signal, "signal"),
              patch.object(ctl.subprocess, "Popen", return_value=process) as start):
            self.assertEqual(ctl.run_container(), 24)
        self.assertEqual(start.call_args.args[0], [ctl.BINDIR / "supervisord", "-n", "-c", ctl.CONF])
        self.assertEqual(start.call_args.kwargs["env"]["FERTILE_ROOT"], str(self.root))
        ready.assert_called_once_with(process)
        self.assertEqual(tuple(reconcile.call_args.args[0]["services"]), ("test",))

    def test_rendered_launcher_applies_the_service_fd_limit(self):
        self.write(command=[sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE))"],
                   limits={"nofile": 640, "stop_seconds": 42})
        programs = self.rendered_programs(ctl.build_plan())
        result = subprocess.run(shlex.split(programs["program:test"]["command"]), cwd=self.root, capture_output=True,
                                text=True, check=True, timeout=5,
                                env={**os.environ, "FERTILE_ROOT": str(self.root)})
        self.assertEqual(result.stdout, "(640, 640)\n")
        self.assertEqual(programs["program:test"]["stopwaitsecs"], "42")

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

    def test_optional_web_settings_are_passed_only_to_web(self):
        rows = [{"name": name, "directory": str(self.root), "command": ["/bin/true"]}
                for name in ("web", "finance-api")]
        self.manifest.write_text(json.dumps({"services": rows}))
        settings = {
            "WEB_PUBLIC_ORIGIN": "https://dashboard.example.test",
            "WEB_ADMIN_SESSION_TTL_SECONDS": "300",
            "WEB_CREDENTIAL_DIR": str(self.root / "jwe"),
            "WEB_CREDENTIAL_RECIPIENT_PUBLIC_KEY_FILE": str(self.root / "recipient.pem"),
        }
        services = ctl.build_plan()["services"]
        self.assertTrue(settings.keys().isdisjoint(services["web"]["env"]))
        (self.root / "config/runtime.env").write_text(
            "".join(f"{key}={value}\n" for key, value in settings.items()))
        with patch.dict(os.environ, {"WEB_ADMIN_SESSION_TTL_SECONDS": "120"}):
            services = ctl.build_plan()["services"]
        expected = {**settings, "WEB_ADMIN_SESSION_TTL_SECONDS": "120"}
        self.assertEqual({key: services["web"]["env"][key] for key in settings}, expected)
        self.assertTrue(settings.keys().isdisjoint(services["finance-api"]["env"]))

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

    def test_controller_routes_external_rollback_and_rechecks_the_url_at_launch(self):
        _, old = self.releases()
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(old)
        (old / "fertile/scripts/supervision.py").write_text("# legacy launcher fixture\n")
        database = f"web_rollback_{old.name}"
        data = self.root / f"data-paper-rollback-{old.name}"
        url = f"postgresql://fixture:synthetic@rollback.invalid/{database}?sslmode=verify-full"
        env = {"PATH": os.defpath, "FERTILE_MARKET_SOURCE": "replay", "FERTILE_DATA_ROOT": str(data),
               "FERTILE_RESEARCH_ROOT": str(data / "research"), "FERTILE_WEB_DATABASE": database,
               "FERTILE_ROLLBACK_EXTERNAL_DB": "1"}
        with (patch.dict(os.environ, env, clear=True), patch.object(ctl.shutil, "which", return_value="/bin/true"),
              patch.object(ctl, "read_env", return_value={"DATABASE_URL": url}) as credentials):
            plan = ctl.build_plan()
            web = plan["services"]["web"]
            self.assertEqual(plan["release"], str(old))
            self.assertEqual(plan["data"], str(data))
            self.assertEqual(plan["web_database"], "external")
            self.assertNotIn("FERTILE_WEB_PEER", plan["services"]["postgres"]["env"])
            self.assertEqual(web["env"]["PGDATABASE"], database)
            self.assertEqual(web["env_file"], "web-rollback-database.env")
            self.assertNotIn("DATABASE_URL", web["env"])
            self.assertEqual(ctl.local_environment(web)["DATABASE_URL"], url)
            self.assertEqual(credentials.call_args_list, [call(self.root / "secrets/web-rollback-database.env", secret=True)] * 2)
            with patch.object(ctl.subprocess, "run") as process:
                ctl.prepare_web(plan)
            process.assert_not_called()
            programs = self.rendered_programs(plan)
            web_launcher = Path(shlex.split(programs["program:web"]["command"])[1])
            finance_launcher = Path(shlex.split(programs["program:finance-api"]["command"])[1])
            self.assertEqual(web_launcher.read_bytes(), (ctl.SCRIPTS / "supervision.py").read_bytes())
            self.assertEqual(finance_launcher.read_text(), "# legacy launcher fixture\n")
            self.assertNotIn(url, ctl.SNAPSHOT.read_text())
            for values in (
                {}, {"DATABASE_URL": url.replace(database, "web")},
                {"DATABASE_URL": f"{url}&%64bname=web"}, {"DATABASE_URL": url, "PGDATABASE": "web"},
            ):
                with self.subTest(keys=list(values)):
                    credentials.return_value = values
                    for action in (ctl.build_plan, lambda: ctl.local_environment(web)):
                        with self.assertRaisesRegex(ValueError, "explicitly select the restored database") as failure:
                            action()
                        self.assertNotIn("synthetic", str(failure.exception))
            credentials.side_effect = FileNotFoundError
            with self.assertRaises(FileNotFoundError):
                ctl.build_plan()
            with self.assertRaises(FileNotFoundError):
                ctl.local_environment(web)

    def test_ordinary_external_database_keeps_existing_configuration_and_rules(self):
        self.releases()
        env = {"PATH": os.defpath, "FERTILE_MARKET_SOURCE": "replay"}
        url = "postgresql://fixture@normal.invalid/web"
        with (patch.dict(os.environ, env, clear=True), patch.object(ctl.shutil, "which", return_value="/bin/true"),
              patch.object(ctl, "check_secret", return_value=""),
              patch.object(ctl, "read_env", return_value={"DATABASE_URL": url}) as credentials):
            plan = ctl.build_plan()
            self.assertEqual(plan["web_database"], "external")
            web = plan["services"]["web"]
            self.assertEqual(web["env_file"], "web-database.env")
            credentials.assert_called_once_with(self.root / "secrets/web-database.env", secret=True)
            self.assertEqual(ctl.local_environment(web)["DATABASE_URL"], url)
            with patch.dict(os.environ, FERTILE_WEB_DATABASE="web_rollback_fixture"):
                with self.assertRaisesRegex(ValueError, "cannot be combined"):
                    ctl.build_plan()

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
