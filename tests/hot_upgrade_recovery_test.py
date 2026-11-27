"""Exercise startup fencing and crash recovery without using an installed stack."""

import configparser
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import handoff_start
import hot_upgrade as journal
import hot_upgrade_cli as cli
import systemd_runtime


class Recovery(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run = self.root / "run"
        self.base = self.root / "releases" / ("a" * 40)
        self.target = self.root / "releases" / ("b" * 40)
        self.base.mkdir(parents=True)
        self.target.mkdir()
        self.logs = self.root / "logs"
        self.snapshot = self.run / "services.json"
        context = patch.multiple(cli.ctl, ROOT=self.root, RUN=self.run, LOGS=self.logs,
                                 SNAPSHOT=self.snapshot, CONF=self.run / "supervisord.conf")
        context.start()
        self.addCleanup(context.stop)
        context = patch.dict(os.environ, {"PATH": os.defpath}, clear=True)
        context.start()
        self.addCleanup(context.stop)

    def interrupted(self, service="finance-backtest-worker", role="worker", source=None):
        def hook(step, _record):
            if step == "compatibility":
                return {"compatible": True}
            raise OSError("synthetic interrupted handoff")

        with self.assertRaisesRegex(RuntimeError, "failed at"):
            journal.perform(self.root, hook, service=service, role=role, previous=self.base.name,
                            target=self.target.name, source=source)
        [record] = journal.records(self.root)
        return SimpleNamespace(id=record["id"], service=service)

    def confirm(self, *, safe_to_start=False):
        return {"reconciled": True, "active_release": self.base.name, "safe_to_start": safe_to_start}

    def plan(self, *services, official=True):
        row = {
            "skip": "", "command": ["/bin/echo", "synthetic-fixture"],
            "release": str(self.base), "env_file": None, "env": {"PATH": os.defpath},
            "directory": str(self.root), "live_url": None, "ready_url": None, "role": "worker",
        }
        return {"default": official, "release": str(self.base), "data": str(self.root),
                "services": {service: deepcopy(row) for service in services}}

    def test_reconcile_bootstraps_stopped_supervisor_only_after_authorization(self):
        args = self.interrupted()
        plan = self.plan(args.service, "finance-collector")
        scripts = self.base / "fertile/scripts"
        scripts.mkdir(parents=True)
        (scripts / "handoff_start.py").touch()
        api = Mock()
        daemon = False
        running = set()
        started = []

        def boot():
            nonlocal daemon
            daemon = True

        def render(value):
            self.snapshot.write_text(json.dumps(value))

        def start(name, wait):
            self.assertTrue(wait)
            release = Path(plan["services"][name]["release"]).name
            handoff_start.require_start(self.root, name, release, "supervisord", launch=True)
            running.add(name)
            started.append(name)

        api.getAllProcessInfo.return_value = []
        api.getProcessInfo.side_effect = lambda name: {
            "pid": 31 if name in running else 0, "statename": "RUNNING" if name in running else "STOPPED",
        }
        api.reloadConfig.return_value = [([], [], [])]
        api.startProcess.side_effect = start
        with (patch.object(cli.units, "managed_release", return_value=None),
              patch.object(cli.ctl, "release_path", return_value=self.base),
              patch.object(cli.ctl, "daemon_pid", side_effect=lambda: int(daemon)),
              patch.object(cli.ctl, "build_plan", side_effect=lambda **_options: deepcopy(plan)),
              patch.object(cli.ctl, "prepare_data_root"),
              patch.object(cli.ctl, "connection", return_value=api),
              patch.object(cli.ctl, "render", side_effect=render),
              patch.object(cli.ctl, "start_daemon", side_effect=boot) as start_daemon):
            with self.assertRaisesRegex(RuntimeError, "could not be reconciled"):
                cli.reconcile(args, Mock(return_value=self.confirm()))
            start_daemon.assert_not_called()
            hook = Mock(side_effect=[self.confirm(safe_to_start=True), {"ready": True}])
            result = cli.reconcile(args, hook)
        start_daemon.assert_called_once_with()
        self.assertEqual(started, [args.service])
        self.assertEqual(result["status"], "reconciled")

    def test_failed_recovery_readiness_cannot_be_replaced_by_a_running_pid(self):
        args = self.interrupted()
        plan = self.plan(args.service)
        manager = Mock()
        manager.pid.return_value = 0
        manager.start.side_effect = lambda *_: setattr(manager.pid, "return_value", 42)
        with (patch.object(cli, "installed", return_value=(self.base, manager)),
              patch.object(cli.ctl, "build_plan", return_value=plan)):
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    hook = Mock(side_effect=[self.confirm(safe_to_start=True), {"ready": False}])
                    with self.assertRaisesRegex(RuntimeError, "could not be reconciled"):
                        cli.reconcile(args, hook)
                    self.assertEqual([c.args[0] for c in hook.call_args_list], ["reconcile", "reconcile-ready"])
                    self.assertEqual(journal.records(self.root)[0]["status"], "needs-reconcile")
            accepted = cli.reconcile(args, Mock(side_effect=[self.confirm(), {"ready": True}]))
        manager.start.assert_called_once_with(args.service, plan["services"][args.service])
        self.assertEqual(accepted["status"], "reconciled")

    def test_paper_cold_recovery_starts_managed_display_and_gateway(self):
        args = self.interrupted("finance-collector", "collector", source="ib")
        plan = self.plan(args.service, "xvfb", "ib-gateway", "healthwatch")
        plan["services"][args.service].update(
            role="collector", source="ib", ready_url="http://127.0.0.1:19082/healthz/ready",
        )
        scripts = self.base / "fertile/scripts"
        scripts.mkdir(parents=True)
        (scripts / "handoff_start.py").touch()
        api = Mock()
        running = {}
        starts = []
        daemon = False
        dependencies = {"xvfb", "ib-gateway"}

        def boot():
            nonlocal daemon
            daemon = True

        def start(name, wait):
            self.assertTrue(wait)
            if name == args.service:
                self.assertTrue(dependencies <= running.keys())
            handoff_start.require_start(self.root, name, self.base.name, "supervisord", launch=True)
            starts.append(name)
            running[name] = 80 + len(starts)

        api.getAllProcessInfo.return_value = []
        api.getProcessInfo.side_effect = lambda name: {
            "pid": running.get(name, 0), "statename": "RUNNING" if name in running else "STOPPED",
        }
        api.reloadConfig.return_value = [([], [], [])]
        api.startProcess.side_effect = start
        with (patch.object(cli.units, "managed_release", return_value=None),
              patch.object(cli.ctl, "release_path", return_value=self.base),
              patch.object(cli.ctl, "daemon_pid", side_effect=lambda: int(daemon)),
              patch.object(cli.ctl, "build_plan", side_effect=lambda **_options: deepcopy(plan)),
              patch.object(cli.ctl, "prepare_data_root"),
              patch.object(cli.ctl, "connection", return_value=api),
              patch.object(cli.ctl, "render", side_effect=lambda row: self.snapshot.write_text(json.dumps(row))),
              patch.object(cli.ctl, "start_daemon", side_effect=boot) as start_daemon,
              patch.object(cli, "owns_listener", return_value=True),
              patch.object(cli.ctl, "http_status", side_effect=lambda *_: 200 if dependencies <= running.keys() else 503)):
            first = Mock(side_effect=[self.confirm(safe_to_start=True), {"ready": False}])
            with self.assertRaisesRegex(RuntimeError, "could not be reconciled"):
                cli.reconcile(args, first)
            self.assertEqual(starts, ["xvfb", "ib-gateway", args.service])
            first_pid = running[args.service]
            del running["ib-gateway"]
            second = Mock(side_effect=[self.confirm(), {"ready": True}])
            self.assertEqual(cli.reconcile(args, second)["status"], "reconciled")
        start_daemon.assert_called_once_with()
        self.assertEqual(starts, ["xvfb", "ib-gateway", args.service, "ib-gateway"])
        self.assertEqual(second.call_args.args[1]["runtime"]["pid"], first_pid)

    def test_replay_and_externally_managed_paper_do_not_start_gateway(self):
        with patch.object(cli.ctl, "daemon_pid", return_value=0):
            manager = cli.Supervisor(self.base, recovery=True)
        manager.api = Mock()
        manager.plan = self.plan("finance-collector", "ib-gateway", "xvfb")
        collector = manager.plan["services"]["finance-collector"]
        gateway = manager.plan["services"]["ib-gateway"]
        for source, skip in (("replay", ""), ("ib", "needs_credentials")):
            with self.subTest(source=source, skip=skip):
                collector["source"] = source
                gateway["skip"] = skip
                manager.prepare_recovery_dependencies("finance-collector")
        manager.api.startProcess.assert_not_called()

    def test_systemd_launcher_blocks_automatic_and_unauthorized_starts(self):
        args = self.interrupted("finance-collector", "collector", source="replay")
        plan = self.plan(args.service, "finance-backtest-worker")
        with (patch.object(cli.ctl, "daemon_pid", return_value=0),
              patch.object(cli.ctl, "check_queue_compatibility"),
              patch.object(cli.ctl, "prepare_data_root") as prepare,
              patch.object(systemd_runtime.os, "chdir"), patch.object(systemd_runtime.os, "execvpe") as execute):
            for phase in ("condition", "prepare", "run"):
                with self.subTest(phase=phase), self.assertRaisesRegex(RuntimeError, "blocks starting"):
                    systemd_runtime.start(plan, phase, args.service)
            prepare.assert_not_called()
            execute.assert_not_called()
            self.assertEqual(systemd_runtime.start(plan, "condition", "finance-backtest-worker"), 0)

            def resume(_step, record):
                with handoff_start.authorize(self.root, record, self.base.name, "systemd") as permit:
                    with self.assertRaisesRegex(RuntimeError, "blocks starting"):
                        handoff_start.require_start(self.root, args.service, self.target.name, "systemd", launch=True)
                    with self.assertRaisesRegex(RuntimeError, "blocks starting"):
                        handoff_start.require_start(self.root, args.service, self.base.name, "supervisord", launch=True)
                    self.assertEqual(systemd_runtime.start(plan, "condition", args.service), 0)
                    systemd_runtime.start(plan, "prepare", args.service)
                    systemd_runtime.start(plan, "run", args.service)
                    handoff_start.wait_for_launch(permit)
                    with self.assertRaisesRegex(RuntimeError, "blocks starting"):
                        systemd_runtime.start(plan, "run", args.service)
                return self.confirm(safe_to_start=True)

            journal.reconcile(self.root, args.id, resume)
        prepare.assert_called_once()
        execute.assert_called_once()

    def test_abandoned_systemd_permit_cannot_start_service(self):
        args = self.interrupted()
        plan = self.plan(args.service)
        script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import handoff_start, hot_upgrade\n"
            "root, txn, release = Path(sys.argv[2]), sys.argv[3], sys.argv[4]\n"
            "def abandon(_, record):\n"
            "    with handoff_start.authorize(root, record, release, 'systemd'):\n"
            "        os._exit(0)\n"
            "hot_upgrade.reconcile(root, txn, abandon)\n"
        )
        subprocess.run([sys.executable, "-B", "-c", script, SCRIPTS, self.root, args.id, self.base.name],
                       check=True, timeout=10)
        permit = handoff_start.ticket_path(self.root, args.id)
        self.assertTrue(permit.is_file())
        with (patch.object(cli.ctl, "daemon_pid", return_value=0),
              patch.object(systemd_runtime.os, "execvpe") as execute):
            with self.assertRaisesRegex(RuntimeError, "blocks starting"):
                systemd_runtime.start(plan, "run", args.service)
        execute.assert_not_called()

    def test_systemd_handoff_claims_its_permit_after_startup_checks(self):
        name = "finance-backtest-worker"
        plan = self.plan(name)
        service = plan["services"][name]
        service["release"] = str(self.target)
        manager = cli.Systemd(self.base)

        def launch(service_name, _service):
            try:
                systemd_runtime.start(plan, "run", service_name)
            except ValueError:
                [record] = journal.records(self.root)
                permit = handoff_start.ticket_path(self.root, record["id"])
                self.assertFalse(json.loads(permit.read_text())["used"])
                raise

        def invoke(step, record):
            if step == "worker-start":
                with patch.object(systemd_runtime, "validate_configured_roots",
                                  side_effect=ValueError("synthetic mount drift")):
                    with self.assertRaisesRegex(ValueError, "mount drift"):
                        cli.start_handoff(manager, name, service, record)
                    execute.assert_not_called()
                cli.start_handoff(manager, name, service, record)
                return {}
            return {"compatibility": {"compatible": True}, "worker-drain": {"drained": True},
                    "worker-stop": {}, "worker-ready": {"ready": True}}[step]

        with (patch.object(cli.ctl, "daemon_pid", return_value=0),
              patch.object(systemd_runtime.os, "chdir"), patch.object(systemd_runtime.os, "execvpe") as execute,
              patch.object(manager, "start", side_effect=launch)):
            result = journal.perform(self.root, invoke, service=name, role="worker",
                                     previous=self.base.name, target=self.target.name)
        self.assertEqual(result["status"], "complete")
        execute.assert_called_once()

    def test_rendered_supervisor_launches_only_with_a_fresh_permit(self):
        args = self.interrupted()
        plan = self.plan(args.service, official=False)
        cli.ctl.render(plan)
        program = configparser.RawConfigParser()
        program.read(self.run / "programs.conf")
        self.assertEqual(program[f"program:{args.service}"]["autostart"], "false")
        argv = shlex.split(program[f"program:{args.service}"]["command"])
        env = {**os.environ, "FERTILE_ROOT": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}

        def launch():
            return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5)

        blocked = launch()
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("blocks starting", blocked.stderr)

        def resume(_step, record):
            with handoff_start.authorize(self.root, record, self.base.name, "supervisord") as permit:
                allowed = launch()
                self.assertEqual(allowed.returncode, 0, allowed.stderr)
                self.assertEqual(allowed.stdout, "synthetic-fixture\n")
                handoff_start.wait_for_launch(permit)
                self.assertNotEqual(launch().returncode, 0)
            return self.confirm(safe_to_start=True)

        journal.reconcile(self.root, args.id, resume)

    def test_legacy_systemd_baseline_cannot_run_a_hot_handoff(self):
        with (patch.object(cli.units, "managed_release", return_value=self.base),
              self.assertRaisesRegex(ValueError, "baseline release with hot-upgrade start fencing")):
            cli.installed()


if __name__ == "__main__":
    unittest.main()
