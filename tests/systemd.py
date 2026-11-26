"""Exercise render, lifecycle and CLI dispatch without using the host systemd manager."""
from contextlib import redirect_stdout
from io import StringIO
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


class Runtime(unittest.TestCase):
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
