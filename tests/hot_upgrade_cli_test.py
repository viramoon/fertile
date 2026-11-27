"""Exercise manager adapters and staged builds using generated local fixtures."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import hot_upgrade as journal
import hot_upgrade_cli as cli
import service_releases as selection


class HostHandoff(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        host = patch.object(cli.ctl, "ROOT", self.root)
        host.start()
        self.addCleanup(host.stop)
        self.base = self.root / "releases" / ("1" * 40)
        self.target = self.root / "releases" / ("2" * 40)
        for release in (self.base, self.target):
            release.mkdir(parents=True)

    def test_reader_candidate_ready_precedes_selected_group_replacement(self):
        manager = Mock()
        candidate = {"live_url": None, "ready_url": None, "env": {"FERTILE_DATA_ROOT": str(self.root)}}
        handoff = cli.Handoff("finance-api", self.base, self.target, candidate, manager, Mock(return_value={}))
        handoff.start_candidate = Mock()
        handoff.close_candidate = Mock()
        handoff.candidate = Mock(pid=51)
        handoff.candidate.poll.return_value = None
        handoff.candidate_plan = dict(candidate)
        with patch.object(cli, "wait_ready", return_value=51) as ready:
            handoff("candidate-start", {})
            handoff("candidate-ready", {})
            self.assertEqual(ready.call_args.args[0](), 51)
            self.assertEqual(manager.mock_calls, [])
            self.assertEqual(selection.selected(self.root, self.base, "finance-api"), self.base)
            handoff("reader-cutover", {})
        self.assertEqual(manager.mock_calls, [call.stop("finance-api"), call.start("finance-api", candidate)])
        self.assertEqual(selection.selected(self.root, self.base, "finance-api"), self.target)
        self.assertEqual(selection.selected(self.root, self.base, "finance-collector"), self.base)
        self.assertEqual(handoff.hook.call_args.args[1]["runtime"],
                         {"pid": 51, "live_url": None, "ready_url": None})

    def test_upgrade_preflights_real_manifest_and_rechecks_queue_after_stop(self):
        name = "finance-api"
        for release in (self.base, self.target):
            manifest = release / "fertile/deploy/services.yaml"
            manifest.parent.mkdir(parents=True)
            shutil.copyfile(REPO / "deploy/services.yaml", manifest)
            (release / "finance").mkdir()
            path = release / "bin" / name
            path.parent.mkdir()
            path.touch()
            path.chmod(0o755)
        args = SimpleNamespace(service=name, release=self.target.name, migrate=False, rollback=False)
        manager = Mock()
        manager.pid.return_value = 201

        def check_queue(_plan):
            if manager.stop.called:
                raise ValueError("late incompatible synthetic job")

        hook = Mock(side_effect=lambda step, _record: {
            "compatibility": {"compatible": True},
            "reader-checkpoint": {"watermark": {}},
            "candidate-ready": {"ready": True, "watermark": {}},
        }[step])

        def start_candidate(handoff):
            handoff.candidate = Mock(pid=41)
            handoff.candidate.poll.return_value = None
            handoff.candidate_plan = {"live_url": None, "ready_url": None}

        with (patch.dict(os.environ, {"PATH": os.defpath, "FERTILE_MARKET_SOURCE": "replay"}, clear=True),
              patch.object(cli, "installed", return_value=(self.base, manager)),
              patch.object(cli, "verify_release"), patch.object(cli, "wait_ready", return_value=41),
              patch.object(cli.Handoff, "start_candidate", start_candidate),
              patch.object(cli.ctl, "check_queue_compatibility",
                                                                         side_effect=check_queue) as queue):
            with self.assertRaisesRegex(RuntimeError, "failed at reader-cutover"):
                cli.upgrade(args, hook)
        self.assertEqual(queue.call_count, 2)
        manager.stop.assert_called_once_with(name)
        manager.start.assert_not_called()
        self.assertEqual(selection.selected(self.root, self.base, name), self.base)

    def test_collector_lease_refusal_leaves_candidate_unstarted(self):
        manager = Mock()

        def hook(step, _record):
            if step == "compatibility":
                return {"compatible": True}
            if step == "collector-seal":
                return {"watermark": {"replay/synthetic": "9007199254740993"}}
            self.assertEqual(step, "collector-release")
            self.assertEqual(manager.mock_calls, [call.stop("finance-collector")])
            return {"released": False}

        handoff = cli.Handoff("finance-collector", self.base, self.target, {}, manager, hook)
        with self.assertRaisesRegex(RuntimeError, "failed at collector-release"):
            journal.perform(self.root, handoff, service="finance-collector", role="collector", source="replay",
                            previous=self.base.name, target=self.target.name)
        self.assertEqual(manager.mock_calls, [call.stop("finance-collector")])
        [record] = journal.records(self.root)
        self.assertEqual(record["gap"]["continuity"], "unknown")
        self.assertEqual(record["status"], "needs-reconcile")
        self.assertEqual(selection.selected(self.root, self.base, "finance-collector"), self.base)

    def test_reconcile_can_restore_only_selected_stopped_service(self):
        def interrupted(step, _record):
            if step == "compatibility":
                return {"compatible": True}
            raise OSError("synthetic failure")

        with self.assertRaisesRegex(RuntimeError, "failed at worker-drain"):
            journal.perform(self.root, interrupted, service="finance-backtest-worker", role="worker",
                            previous=self.base.name, target=self.target.name)
        [record] = journal.records(self.root)
        service = record["service"]
        args = SimpleNamespace(id=record["id"])
        manager = Mock()
        manager.pid.return_value = 0
        manager.start.side_effect = lambda *_: setattr(manager.pid, "return_value", 42)
        row = {"skip": "", "live_url": None, "ready_url": None}
        answer = {"reconciled": True, "active_release": self.base.name}
        with (patch.object(cli, "installed", return_value=(self.base, manager)),
              patch.object(cli.ctl, "build_plan", return_value={"services": {service: row}}),
              patch.object(cli, "wait_ready", return_value=42) as ready):
            with self.assertRaisesRegex(RuntimeError, "could not be reconciled"):
                cli.reconcile(args, Mock(return_value=answer))
            manager.start.assert_not_called()
            hook = Mock(side_effect=[{**answer, "safe_to_start": True}, {"ready": True}])
            result = cli.reconcile(args, hook)
        self.assertEqual([item.args[0] for item in hook.call_args_list], ["reconcile", "reconcile-ready"])
        manager.start.assert_called_once_with(service, row)
        ready.assert_called_once()
        self.assertEqual(result["status"], "reconciled")

    def test_supervisor_reload_changes_only_selected_group(self):
        api = Mock()
        plan = {"release": str(self.base), "default": True,
                "services": {"finance-api": {"release": str(self.base)}, "finance-collector": {"pid": 88}}}
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan))
        api.reloadConfig.return_value = [[[], ["finance-api", "finance-collector"], []]]
        with (patch.object(cli.ctl, "connection", return_value=api),
              patch.object(cli.ctl, "active_programs"), patch.object(cli.ctl, "SNAPSHOT", path),
              patch.object(cli.ctl, "render") as render):
            manager = cli.Supervisor(self.base)
            manager.start("finance-api", {"release": str(self.target)})
        self.assertEqual(render.call_args.args[0]["services"]["finance-collector"], {"pid": 88})
        self.assertEqual(api.mock_calls, [call.reloadConfig(), call.removeProcessGroup("finance-api"),
                                          call.addProcessGroup("finance-api"), call.startProcess("finance-api", True)])

    def test_systemd_targets_owned_units_and_waits_for_stop(self):
        name = "finance-collector"
        unit = cli.units.service_unit(name)
        state = {"FragmentPath": str(cli.units.UNIT_DIR / unit), "ActiveState": "active", "MainPID": "201"}

        def systemctl(verb, called_unit, **_kwargs):
            self.assertEqual(called_unit, unit)
            if verb == "stop":
                state.update(ActiveState="inactive", MainPID="0")

        marker = {"root": str(self.root), "release": self.base.name}
        with (patch.object(cli.units, "matching_unit", return_value=marker),
              patch.object(cli.units, "unit_state", side_effect=lambda _: {unit: state}),
              patch.object(cli.units, "systemctl", side_effect=systemctl) as control):
            manager = cli.Systemd(self.base)
            self.assertEqual(manager.pid(name), 201)
            manager.stop(name)
            self.assertEqual(manager.pid(name), 0)
            manager.start(name, {})
            self.assertEqual([c.args for c in control.call_args_list], [("stop", unit), ("start", unit)])
            marker["release"] = self.target.name
            with self.assertRaisesRegex(ValueError, "outside"):
                manager.stop(name)
            self.assertEqual(control.call_count, 2)

    def test_rollback_to_baseline_keeps_explicit_pin(self):
        selection.select(self.root, self.base, "web", self.target)
        self.assertIsNone(selection.pinned(self.root, self.target, "web"))
        selection.select(self.root, self.base, "web", self.base)
        self.assertEqual(selection.pinned(self.root, self.base, "web"), self.base)
        self.assertEqual(selection.selections(self.root, self.base), {"web": self.base.name})

    def test_hook_round_trip_uses_argv_and_json(self):
        executable = self.make_hook(
            "import json, os, sys\n"
            "record = json.load(sys.stdin)\n"
            "print(json.dumps({'argv': sys.argv[1], 'step': record['step'], "
            "'service': record['service'], 'root': os.environ['FERTILE_ROOT']}))\n"
        )
        reply = cli.hook_command(str(executable))("compatibility", {"service": "finance-api"})
        self.assertEqual(reply, {"argv": "compatibility", "step": "compatibility",
                                 "service": "finance-api", "root": str(self.root)})

    def make_hook(self, body):
        executable = self.root / "fixture-hook"
        executable.write_text(f"#!{sys.executable}\n{body}")
        executable.chmod(0o700)
        return executable

    def test_hook_rejects_non_objects(self):
        for output in ("null", "[]", "true", '"value"'):
            with self.subTest(output=output):
                executable = self.make_hook(f"print({output!r})\n")
                with self.assertRaisesRegex(ValueError, "JSON object"):
                    cli.hook_command(str(executable))("reader-cutover", {})

    def test_hook_rejects_oversize_multibyte_reply(self):
        output = json.dumps({"value": "中" * 25_000}, ensure_ascii=False)
        self.assertLess(len(output), 64 * 1024)
        self.assertGreater(len(output.encode("utf-8")), 64 * 1024)
        executable = self.make_hook(
            "import sys\n"
            f"sys.stdout.buffer.write({output.encode('utf-8')!r})\n"
        )
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            cli.hook_command(str(executable))("candidate-ready", {})

    def test_hook_accepts_exact_byte_limit(self):
        length = 64 * 1024 - len(json.dumps({"value": ""}).encode("utf-8"))
        executable = self.make_hook(
            "import json, sys\n"
            f"sys.stdout.buffer.write(json.dumps({{'value': 'a' * {length}}}).encode())\n"
        )
        self.assertEqual(cli.hook_command(str(executable))("candidate-ready", {}), {"value": "a" * length})

    def test_projector_rejects_unacknowledged_cutover_before_stopping_incumbent(self):
        for reply in (None, [], {"stopped": False}):
            with self.subTest(reply=reply):
                manager = Mock()
                handoff = cli.Handoff("finance-projector", self.base, self.target, {}, manager,
                                      Mock(return_value=reply))
                with self.assertRaisesRegex(ValueError, "empty JSON object"):
                    handoff("reader-cutover", {})
                self.assertEqual(manager.mock_calls, [])
                self.assertEqual(selection.selected(self.root, self.base, "finance-projector"), self.base)

    def test_status_is_read_only_for_empty_root(self):
        stdout = StringIO()
        with patch.object(sys, "argv", ["fertile", "hot-upgrade-status", "--json"]), redirect_stdout(stdout):
            cli.main()
        self.assertEqual(json.loads(stdout.getvalue()), [])
        self.assertFalse((self.root / "state").exists())


class StageBuild(unittest.TestCase):
    def test_stage_only_reuses_verified_release_without_changing_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            env = {**os.environ, "FERTILE_ROOT": str(root), "FERTILE_SOURCE_DIR": str(repo),
                   "GIT_ALLOW_PROTOCOL": "file", "GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
                   "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}

            def git(*args):
                return subprocess.check_output(["git", "-C", repo, *args], env=env, text=True).strip()

            git("init", "-q")
            pins = ["a" * 40, "b" * 40, "c" * 40]
            for name, pin in zip(("finance", "web", "frontend"), pins):
                git("update-index", "--add", "--cacheinfo", f"160000,{pin},{name}")
            git("commit", "-qm", "Synthetic release inputs")
            revision = git("rev-parse", "HEAD")
            releases = root / "releases"
            current = root / "current"
            (releases / "previous").mkdir(parents=True)
            current.symlink_to("releases/previous")
            release = releases / revision

            def fixture(name, content="", executable=False):
                file = release / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content)
                if executable:
                    file.chmod(0o755)

            for name in ("bin/finance-api", "bin/finance-collector", "bin/finance-ib", "bin/web",
                         "finance/scripts/finance-backtest-worker"):
                fixture(name, executable=True)
            for name in ("static/index.html", "web/config/base.yaml", "finance/sql/backtest-queue.sql",
                         "finance/pyproject.toml", "finance/vendor/nautilus_trader/pyproject.toml",
                         "finance/vendor/nautilus_trader/examples/backtest/example_01_load_bars_from_custom_csv/run_example.py",
                         "fertile/VERSION", "finance/scripts/verify-source.py"):
                fixture(name)
            source = "d" * 40
            fixture("finance/toolchain.env", f"NAUTILUS_COMMIT={source}\n")
            fixture("runtime/venv/bin/python", '#!/bin/sh\n'
                    'if [ "${2:-}" = distribution ]; then echo \'{"wheel":"fixture.whl","sha256":null}\'; fi\n',
                    executable=True)
            wheel = root / "fixture.whl"
            wheel.write_text("Synthetic wheel identifier fixture\n")
            subprocess.run([sys.executable, REPO / "scripts/release-manifest.py", "create", release, revision,
                            *pins, source, wheel], check=True, env=env)
            result = subprocess.run([REPO / "scripts/build-release.sh", "--stage-only", "--ref", revision],
                                    check=True, env=env, capture_output=True, text=True)
            self.assertIn(f"Staged {release}", result.stdout)
            self.assertEqual(os.readlink(current), "releases/previous")
            result = subprocess.run([REPO / "scripts/build-release.sh", "--ref", revision],
                                    check=True, env=env, capture_output=True, text=True)
            self.assertIn(f"Activated {release}", result.stdout)
            self.assertEqual(os.readlink(current), f"releases/{revision}")


if __name__ == "__main__":
    unittest.main()
