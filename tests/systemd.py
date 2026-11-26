"""Exercise render, lifecycle and CLI dispatch without using the host systemd manager."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import systemd_units as units
import systemd_runtime as runtime
import yaml


class Render(unittest.TestCase):
    def test_target_orders_shutdown_after_every_generated_member(self):
        rendered = units.render(Path("/srv/fertile"), "a" * 40, "trader", "trading",
                                include_optional=True, settings={}, mounts=set())
        target = rendered[units.TARGET]
        members = [unit for unit in rendered if unit != units.TARGET]
        self.assertIn("DefaultDependencies=no\n", target)
        self.assertIn(f"Before={' '.join(members)} shutdown.target\n", target)
        self.assertIn("Conflicts=shutdown.target\n", target)
        self.assertNotIn("\nAfter=", target)
        for unit in members:
            self.assertIn("PartOf=fertile.target\n", rendered[unit])
        units.validate_units(rendered)

    def test_validation_rejects_target_stop_order_that_returns_before_members(self):
        rendered = units.render(Path("/srv/fertile"), "a" * 40, "trader", "trading",
                                settings={}, mounts=set())
        target = rendered[units.TARGET]
        for replacement in (
            target.replace("Before=", "After="),
            target.replace("DefaultDependencies=no", "DefaultDependencies=yes"),
        ):
            with self.subTest(target=replacement), self.assertRaisesRegex(ValueError, "shutdown ordering"):
                units.validate_units({**rendered, units.TARGET: replacement})

    def test_fstab_mount_uses_same_canonical_path_as_volume(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            mount = base / "data mount"
            mount.mkdir()
            alias = base / "mount-alias"
            alias.symlink_to(mount, target_is_directory=True)
            output = json.dumps({"filesystems": [{"target": str(alias)}]})
            found = subprocess.CompletedProcess([], 0, output, "")
            with patch.object(units.subprocess, "run", return_value=found):
                self.assertEqual(units.declared_mounts(), {mount.resolve()})

    def test_uses_configured_data_and_research_volume_dependencies(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "instance"
            config = root / "config"
            config.mkdir(parents=True)
            mount = base / "data disk"
            data = mount / "fertile-data"
            research = data / "research"
            (config / "runtime.env").write_text(
                f'FERTILE_MARKET_SOURCE=replay\nFERTILE_DATA_ROOT="{data}"\n'
                f'FERTILE_RESEARCH_ROOT="{research}"\n'
            )
            with patch.dict(os.environ, FERTILE_DATA_ROOT=str(base / "ambient")):
                rendered = units.render(root, "a" * 40, "trader", "trading", mounts={mount})
            units.validate_units(rendered)
            collector = rendered["fertile-finance-collector.service"]
            self.assertIn(f'RequiresMountsFor="{root}" "{data}"\n', collector)
            self.assertIn(f'Environment="FERTILE_SYSTEMD_DATA_ROOT={data}"\n', collector)
            self.assertIn(units.quote(f"FERTILE_SYSTEMD_MOUNTS={json.dumps([str(mount)])}"), collector)
            self.assertIn(f'RequiresMountsFor="{root}" "{data}" "{research}"\n',
                          rendered["fertile-finance-api.service"])
            self.assertNotIn(str(data), rendered["fertile-web.service"].split("[Service]")[0])

    def test_replay_uses_its_own_data_path_by_default(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rendered = units.render(root, "a" * 40, "trader", "trading",
                                    settings={"FERTILE_MARKET_SOURCE": "replay"}, mounts=set())
            self.assertIn(f'RequiresMountsFor="{root}" "{root}/data/replay"\n',
                          rendered["fertile-finance-collector.service"])

    def test_configured_external_root_without_known_parent_requires_mount(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "instance"
            data = base / "unmounted-volume"
            data.mkdir()
            rendered = units.render(root, "a" * 40, "trader", "trading",
                                    settings={"FERTILE_DATA_ROOT": str(data)}, mounts=set())
            self.assertIn(units.quote(f"FERTILE_SYSTEMD_MOUNTS={json.dumps([str(data)])}"),
                          rendered["fertile-finance-collector.service"])

    def test_installer_storage_config_rejects_symlink_and_nonregular_files(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "instance"
            root.mkdir()
            public = base / "fixture-config"
            public.mkdir()
            config = root / "config"
            config.symlink_to(public)
            with self.assertRaises(OSError):
                units.storage_settings(root)
            config.unlink()
            config.mkdir()
            runtime_settings = config / "runtime.env"
            fixture = public / "fixture.env"
            fixture.write_text("FERTILE_MARKET_SOURCE=replay\n")
            runtime_settings.symlink_to(fixture)
            with self.assertRaises(OSError):
                units.storage_settings(root)
            runtime_settings.unlink()
            os.mkfifo(runtime_settings)
            with self.assertRaisesRegex(ValueError, "regular file"):
                units.storage_settings(root)

    def test_templates_follow_core_manifest_order_and_escape_settings(self):
        root = Path('/srv/fertile "%i"$stack')
        rendered = units.render(root, "a" * 40, "trader", "trading")
        manifest = yaml.safe_load((REPO / "deploy/services.yaml").read_text())["services"]
        self.assertEqual(tuple(rendered), (*map(units.service_unit, [row["name"] for row in manifest[:5]]), units.TARGET))
        units.validate_units(rendered)
        previous = None
        for name in units.SERVICES:
            unit = units.service_unit(name)
            content = rendered[unit]
            self.assertIn("User=trader\nGroup=trading", content)
            self.assertIn('Environment="FERTILE_ROOT=/srv/fertile \\"%%i\\"$stack"', content)
            self.assertIn(f'ExecStart=:/bin/bash "{units.quote(root)[1:-1]}/releases/', content)
            self.assertIn(f" run {name}", content)
            if previous:
                self.assertIn(f"After={previous}", content)
            previous = unit
        self.assertIn("ExecStartPost=", rendered["fertile-postgres.service"])
        with self.assertRaisesRegex(ValueError, "non-root"):
            units.render(root, "a" * 40, "root", "trading")
        for value in ("relative", "/", "/tmp/..", "/tmp/invalid\nroot"):
            with self.subTest(root=value), self.assertRaises(ValueError):
                units.installation_root(value)

    def test_verification_missing_analyzer_and_genuine_failure(self):
        rendered = units.render(Path("/srv/fertile"), "a" * 40, "trader", "trading")
        output = StringIO()
        with patch.object(units.shutil, "which", return_value=None), redirect_stdout(output):
            units.verify(rendered)
        self.assertIn("systemd-analyze unavailable", output.getvalue())
        invalid = dict(rendered, **{"fertile-web.service": rendered["fertile-web.service"].replace("User=", "UserX=")})
        with self.assertRaisesRegex(ValueError, "incomplete service"):
            units.validate_units(invalid)
        failure = subprocess.CompletedProcess(["systemd-analyze"], 1, "", "")
        with (patch.object(units.shutil, "which", return_value="/usr/bin/systemd-analyze"),
              patch.object(units.subprocess, "run", return_value=failure)):
            with self.assertRaises(subprocess.CalledProcessError):
                units.verify(rendered)

    def test_optional_artifacts_render_units_with_shared_limits_and_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            commit = "c" * 40
            binary = root / "releases" / commit / "bin"
            binary.mkdir(parents=True)
            for name in ("finance-market-wal", "web-notifier"):
                (binary / name).touch()
            rendered = units.render(root, commit, "trader", "trading")
            units.validate_units(rendered)
            projector = rendered["fertile-finance-projector.service"]
            self.assertIn("After=fertile-finance-collector.service\n", projector)
            self.assertIn(f'{root}/data/finance-state/market-wal"', projector)
            self.assertIn("TimeoutStopSec=60s\nLimitNOFILE=4096", projector)
            notifier = rendered["fertile-web-notifier.service"]
            self.assertIn("After=fertile-web.service fertile-postgres.service\n", notifier)
            self.assertIn("TimeoutStopSec=75s\nLimitNOFILE=1024", notifier)
            self.assertIn(" condition web-notifier\n", notifier)
            self.assertNotIn("Requisite=fertile-web.service", notifier)
            self.assertNotIn("fertile-finance-live-decision.service", rendered)
            self.assertIn("fertile-web-notifier.service", rendered[units.TARGET])


class Lifecycle(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "root"
        self.directory = self.base / "units"
        self.directory.mkdir()
        self.boot = self.base / "system"
        self.boot.mkdir()
        self.commit = "a" * 40
        self.fragment = {}
        self.state = {}
        self.operations = []
        fake_os = SimpleNamespace(**vars(os))
        fake_os.geteuid = lambda: 0
        fake_os.fstat = lambda fd: Mock(st_uid=0, st_mode=os.fstat(fd).st_mode)
        self.stack = [
            patch.object(units, "UNIT_DIR", self.directory), patch.object(units, "SYSTEMD_BOOT", self.boot),
            patch.object(units, "os", fake_os), patch.object(units, "systemctl", side_effect=self.systemctl),
            patch.object(units, "selected_release", return_value=self.commit),
            patch.object(units.pwd, "getpwnam", return_value=Mock(pw_uid=1000)),
            patch.object(units.grp, "getgrnam", return_value=Mock(gr_gid=1000)),
        ]
        for context in self.stack:
            context.start()
            self.addCleanup(context.stop)

    def systemctl(self, action, *args, **kwargs):
        self.operations.append((action, *args))
        if action == "show":
            fields = []
            for name in args[2:]:
                fragment = self.fragment.get(name, "")
                state = self.state.get(name, "inactive")
                load_state = "loaded" if fragment else "not-found"
                fields.append(f"Id={name}\nLoadState={load_state}\nFragmentPath={fragment}\nActiveState={state}\nMainPID=0\n")
            return subprocess.CompletedProcess(["systemctl"], 0, "\n".join(fields), "")
        if action == "daemon-reload":
            self.fragment = {path.name: str(path) for path in self.directory.iterdir()}
        return subprocess.CompletedProcess(["systemctl"], 0, "", "")

    def install(self):
        with redirect_stdout(StringIO()):
            units.install(self.root, "trader", "trading")

    def test_install_enable_restart_log_and_remove_only_managed_units(self):
        self.install()
        self.assertEqual(self.operations[-2:], [("daemon-reload",), ("enable", units.TARGET)])
        self.assertEqual(stat.S_IMODE((self.directory / units.TARGET).stat().st_mode), 0o644)
        self.assertEqual(units.managed_release(self.root), self.root / "releases" / self.commit)
        self.assertIsNone(units.managed_release(self.base / "other"))
        with patch.object(units.subprocess, "run") as journal:
            units.service_command(self.root, "logs", "web")
            self.assertEqual(journal.call_args.args[0][-2:], ["-u", "fertile-web.service"])
        units.service_command(self.root, "restart", "finance-api")
        self.assertEqual(self.operations[-1], ("restart", "fertile-finance-api.service"))
        for name in ["../foreign", "ib-gateway"]:
            with self.assertRaisesRegex(ValueError, "service unavailable"):
                units.service_command(self.root, "restart", name)
        retained = self.directory / "outside.service"
        retained.write_text("not managed")
        units.uninstall(self.root)
        self.assertEqual(set(self.directory.iterdir()), {retained})
        self.assertEqual(self.operations[-3][0], "stop")
        self.assertEqual(self.operations[-2:], [("disable", units.TARGET), ("daemon-reload",)])

    def test_conflicts_do_not_replace_units_or_follow_symlinks(self):
        unit = self.directory / "fertile-web.service"
        other = self.base / "unrelated.txt"
        other.write_text("control")
        unit.symlink_to(other)
        for task in (self.install, lambda: units.uninstall(self.root)):
            with self.assertRaises(OSError):
                task()
        self.assertEqual(other.read_text(), "control")
        self.assertEqual(self.operations, [])
        unit.unlink()
        self.fragment[unit.name] = str(self.base / "foreign" / unit.name)
        with self.assertRaisesRegex(ValueError, "another deployment"):
            self.install()
        self.assertEqual(list(self.directory.iterdir()), [])
        self.fragment.clear()
        self.install()
        with self.assertRaisesRegex(ValueError, "another Fertile root"):
            units.uninstall(self.base / "different")
        self.assertTrue((self.directory / units.TARGET).is_file())

    def test_refuse_live_replacement_and_fail_closed_on_manager_mismatch(self):
        self.install()
        self.state[units.TARGET] = "active"
        with patch.object(units, "selected_release", return_value="b" * 40):
            with self.assertRaisesRegex(RuntimeError, "stop fertile.target"):
                self.install()
        self.assertEqual(units.managed_release(self.root).name, self.commit)
        self.fragment[units.TARGET] = str(self.base / "foreign" / units.TARGET)
        with self.assertRaisesRegex(RuntimeError, "not using them"):
            units.managed_release(self.root)
        self.boot.rmdir()
        self.assertIsNone(units.managed_release(self.root))

    def test_install_removes_only_retired_owned_optional_units(self):
        binary = self.root / "releases" / self.commit / "bin"
        binary.mkdir(parents=True)
        notifier_bin = binary / "web-notifier"
        notifier_bin.touch()
        self.install()
        notifier = self.directory / "fertile-web-notifier.service"
        self.assertTrue(notifier.is_file())
        notifier_bin.unlink()
        self.install()
        self.assertFalse(notifier.exists())
        self.assertNotIn(notifier.name, (self.directory / units.TARGET).read_text())

    def test_cli_restart_checks_shared_journal_before_contacting_manager(self):
        journal = self.root / "state/hot-upgrade"
        journal.mkdir(parents=True)
        record_id = "1" * 32
        entry = journal / f"{record_id}.json"
        entry.write_text(json.dumps({"id": record_id, "version": 1, "status": "running",
                                     "started_at": "2026-01-01T00:00:00Z"}))
        argv = ["fertile", "_restart", "--root", str(self.root), "web"]
        with (patch.object(sys, "argv", argv), patch.object(units, "service_command") as dispatch,
              patch.object(units.os, "geteuid", return_value=1000)):
            with self.assertRaisesRegex(RuntimeError, "unfinished hot-upgrade"):
                units.main()
            dispatch.assert_not_called()
            entry.unlink()
            self.assertEqual(units.main(), 0)
            dispatch.assert_called_once_with(self.root, "restart", "web")
        self.assertTrue((self.root / "run/control.lock").is_file())


class Runtime(unittest.TestCase):
    def test_installed_storage_guard_checks_mount_not_directory_and_rejects_drift(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            mount = base / "mount point"
            data = mount / "data"
            research = data / "research"
            research.mkdir(parents=True)
            mountinfo = base / "mountinfo"
            mountinfo.write_text("1 0 0:1 / / rw - tmpfs synthetic rw\n")
            name = "finance-collector"
            service = {"release": str(base / "releases" / ("a" * 40)), "skip": "",
                       "env": {"FERTILE_RESEARCH_ROOT": str(research)}}
            plan = {"data": str(data), "services": {name: service}}
            env = {"FERTILE_SYSTEMD_DATA_ROOT": str(data), "FERTILE_SYSTEMD_RESEARCH_ROOT": str(research),
                   "FERTILE_SYSTEMD_MOUNTS": json.dumps([str(mount)])}
            with (patch.dict(os.environ, env, clear=True), patch.object(units, "MOUNTINFO", mountinfo),
                  patch.object(runtime.ctl, "ROOT", base), patch.object(runtime.ctl, "RUN", base / "run"),
                  patch.object(runtime.ctl, "daemon_pid", return_value=0), patch.object(runtime.os, "execvpe") as launch):
                with self.assertRaisesRegex(RuntimeError, "required systemd mount is unavailable"):
                    runtime.start(plan, "run", name)
                launch.assert_not_called()
                escaped = str(mount).replace(" ", "\\040")
                with mountinfo.open("a") as stream:
                    stream.write(f"2 1 0:1 / {escaped} rw - tmpfs synthetic rw\n")
                units.validate_configured_roots(plan, name)
                with self.assertRaisesRegex(ValueError, "storage roots changed"):
                    runtime.start({**plan, "data": str(base / "different")}, "run", name)
                launch.assert_not_called()

    def test_runtime_plan_excludes_optional_services_and_probes_configured_ports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "releases" / ("a" * 40)
            scripts = release / "fertile/scripts"
            config = release / "finance/config/collector"
            for path in (scripts, release / "fertile/deploy", release / "web", config, root / "config"):
                path.mkdir(parents=True)
            shutil.copy(REPO / "deploy/services.yaml", release / "fertile/deploy/services.yaml")
            shutil.copy(REPO / "VERSION", release / "fertile/VERSION")
            (scripts / "prepare-web-db.sql").touch()
            (config / "stable.yaml").write_text("source: replay\n")
            (root / "config/runtime.env").write_text("FERTILE_PROFILE=stable\nFERTILE_MARKET_SOURCE=replay\n"
                                                   "FINANCE_API_PORT=19081\nWEB_PORT=19090\n")
            states = {unit: {"ActiveState": "active", "MainPID": "5",
                             "FragmentPath": str(units.UNIT_DIR / unit)}
                      for unit in [units.TARGET, *map(units.service_unit, units.SERVICES)]}
            ctl = runtime.ctl
            with (patch.object(ctl, "ROOT", root), patch.object(ctl, "RUN", root / "run"),
                  patch.object(ctl, "SCRIPTS", scripts), patch.dict(os.environ, {"PATH": os.defpath}, clear=True),
                  patch.object(ctl.shutil, "which", return_value="/bin/true"),
                  patch.object(ctl, "check_secret", return_value="needs_credentials") as check,
                  patch.object(runtime, "unit_state", return_value=states),
                  patch.object(ctl, "http_status", side_effect=lambda url, agent: 200 if url else None)):
                plan = runtime.plan_for_release()
                report = runtime.report(plan)
            check.assert_called_once_with(root / "secrets/web-database.env")
        self.assertEqual(report["manager"], "systemd")
        self.assertEqual(tuple(report["services"]), units.SERVICES)
        self.assertEqual(report["services"]["finance-api"]["ready_url"], "http://127.0.0.1:19081/healthz/ready")
        self.assertEqual(report["services"]["web"]["live_url"], "http://127.0.0.1:19090/healthz/live")
        self.assertEqual(report["services"]["web"]["live"], 200)


class Cli(unittest.TestCase):
    def test_dispatches_systemd_and_supervisord_from_real_entry_point(self):
        # Run the unchanged shell entry point from a fixture checkout. The
        # stand-in control module simulates manager detection and systemctl.
        with TemporaryDirectory() as directory:
            base = Path(directory)
            cli = base / "scripts"
            cli.mkdir()
            for name in ("fertile", "runtime-root.sh"):
                shutil.copy2(REPO / "scripts" / name, cli / name)
            (cli / "systemd_units.py").write_text(
                "import os, sys\n"
                "action = sys.argv[1]\n"
                "if action == '_managed':\n"
                "    outcome = int(os.environ['FIXTURE_MANAGER'])\n"
                "    if outcome: sys.exit(outcome)\n"
                "    print(os.environ['FERTILE_ROOT'] + '/release')\n"
                "else: print(action, sys.argv[-1])\n"
            )
            root = base / "root"
            release_scripts = root / "release/fertile/scripts"
            release_scripts.mkdir(parents=True)
            (release_scripts / "systemd-service.sh").write_text('printf "%s %s\\n" "$1" "$2"\n')
            env = {"FERTILE_ROOT": str(root), "FERTILE_CONTROL_PYTHON": "/bin/echo", "PATH": os.defpath}
            for action, arg in [("status", "--json"), ("restart", "web"), ("logs", "finance-api")]:
                with self.subTest(action=action):
                    result = subprocess.run([cli / "fertile", action, arg], text=True, capture_output=True,
                                            env={**env, "FIXTURE_MANAGER": "3"}, check=True)
                    self.assertEqual(result.stdout.strip(), f"{cli}/supervision.py {action} {arg}")
                    # Privileged manager commands must dispatch before reading env.sh.
                    (root / "env.sh").write_text("exit 88\n")
                    result = subprocess.run([cli / "fertile", action, arg], text=True, capture_output=True,
                                            env={**env, "FIXTURE_MANAGER": "0"})
                    if os.geteuid() == 0 and action == "status":
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("as the service account", result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        prefix = "" if action == "status" else "_"
                        self.assertEqual(result.stdout.strip(), f"{prefix}{action} {arg}")
                    closed = subprocess.run([cli / "fertile", action, arg], text=True, capture_output=True,
                                            env={**env, "FIXTURE_MANAGER": "1"})
                    self.assertEqual(closed.returncode, 1)
                    (root / "env.sh").unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
