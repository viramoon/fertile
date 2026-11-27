"""Check the migration rollback guide without a database, services or network."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = "ops/ruler-runbook.md"
RECOVERY = "从升级前快照回滚并切回新版"
SHA = "0123456789abcdef0123456789abcdef01234567"


def section(document, heading):
    return re.search(rf"^##+ {re.escape(heading)}\n(.*?)(?=^##+ |\Z)",
                     (ROOT / document).read_text(), re.MULTILINE | re.DOTALL)[1]


class RollbackPolicyTests(unittest.TestCase):
    def test_migration_rollback_guides_require_pre_migration_restore(self):
        for document, heading in (
            (RUNBOOK, RECOVERY),
            (RUNBOOK, "v1 T3a 首批运行时核对"),
            ("docs/v1-integration.md", "含迁移发布的回退"),
            ("deploy/hot-upgrade.md", "升级与兼容回退"),
        ):
            with self.subTest(document=document, heading=heading):
                guide = section(document, heading)
                self.assertRegex(guide, r"迁移前备份[^。]*`data-paper-rollback-<sha>`[^。]*`web_rollback_<sha>`")
                self.assertRegex(guide, r"`web_rollback_<sha>`[^。]*[。 ，][^。]*运行旧发布")
                self.assertNotIn("WEB_AUTO_MIGRATE", guide)
                if heading != RECOVERY:
                    self.assertIn(f"#{RECOVERY})", guide)

    def test_shared_rollback_requires_no_migration_or_new_release_writes(self):
        for document, heading in (
            (RUNBOOK, "整栈维护"),
            ("deploy/hot-upgrade.md", "升级与兼容回退"),
        ):
            with self.subTest(document=document):
                self.assertRegex(section(document, heading), r"`(?:--)?rollback`[^。]*无新增迁移[^。]*未被新版写过")


class RollbackExampleTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.trace = self.root / "trace.jsonl"
        self.instance = self.root / "instance"
        snapshot = self.root / "before-migration"
        snapshot.mkdir()
        self.dump = snapshot / "postgres.dump"
        self.dump.write_text("synthetic archive marker\n")
        self.target_data = self.instance / f"data-paper-rollback-{SHA}"
        self.target_db = f"web_rollback_{SHA}"
        self.protected = [self.instance / path for path in ("data-paper/sentinel", "config/runtime.env", "env.sh")]
        for path in self.protected:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic active instance marker\n")
        tools = self.root / "bin"
        tools.mkdir()
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.env = {
            "PATH": str(tools), "TRACE": str(self.trace), "FERTILE_ROOT": str(self.instance),
            "FERTILE_POSTGRES_PORT": "5432", "old_sha": SHA, "pre_upgrade": str(snapshot),
            "PGHOSTADDR": "192.0.2.1", "PGSERVICE": "wrong_service", "FERTILE_WEB_DATABASE": "web",
            "FERTILE_DATA_ROOT": str(self.instance / "data-paper"),
        }
        for command in ("env", "id"):
            (tools / command).symlink_to(shutil.which(command))
        stub = self.root / "record-command"
        stub.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys

command, *args = sys.argv
name = Path(command).name
keys = ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGPORT", "FERTILE_DATA_ROOT", "FERTILE_RESEARCH_ROOT",
        "FERTILE_WEB_DATABASE", "FERTILE_ENABLE_WEB_NOTIFIER", "FERTILE_BACKUP_REMOTE")
with Path(os.environ["TRACE"]).open("a") as trace:
    trace.write(json.dumps([name, args, {key: os.getenv(key) for key in keys}]) + "\\n")
if name == "psql":
    print(os.getenv("OCCUPIED", ""))
if name == "restore.sh" and os.getenv("FAIL_RESTORE") == ("dry-run" if "--dry-run" in args else "restore"):
    sys.exit(1)
''')
        stub.chmod(0o700)
        (tools / "psql").symlink_to(stub)
        for command in ("restore.sh", "postgres.sh", "fertile"):
            (scripts / command).symlink_to(stub)

    def run_example(self, *, status, **env):
        block, = re.findall(r"^ +```bash\n(.*?)^ +```", section(RUNBOOK, RECOVERY), re.MULTILINE | re.DOTALL)
        result = subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-c", block], cwd=self.root,
                                env=self.env | env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, status, result.stderr)
        for path in self.protected:
            self.assertEqual(path.read_text(), "synthetic active instance marker\n")
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_example_restores_before_launch_with_matching_isolated_targets(self):
        trace = self.run_example(status=0)
        target = ["--target-data-root", str(self.target_data), "--target-database", self.target_db]
        snapshot = self.env["pre_upgrade"]
        self.assertEqual([(command, args) for command, args, _ in trace], [
            ("psql", ["-XAt", "-v", "ON_ERROR_STOP=1", "-d", "postgres",
                      "-c", f"SELECT 1 FROM pg_database WHERE datname = '{self.target_db}'"]),
            ("restore.sh", [snapshot, "--dry-run", *target]),
            ("restore.sh", [snapshot, *target]),
            ("postgres.sh", ["stop"]),
            ("fertile", ["rollback", "--switch-only", SHA]),
            ("fertile", ["ensure-up"]),
            ("fertile", ["data-root"]),
            ("fertile", ["status", "--json"]),
        ])
        for _, _, env in trace[:3]:
            self.assertEqual(env["PGHOST"], str(self.instance / "run/postgres"))
            self.assertEqual(env["PGPORT"], "5432")
            self.assertIsNone(env["PGHOSTADDR"])
            self.assertIsNone(env["PGSERVICE"])
        for _, _, env in trace[4:]:
            self.assertEqual(env["FERTILE_DATA_ROOT"], str(self.target_data))
            self.assertEqual(env["FERTILE_RESEARCH_ROOT"], str(self.target_data / "research"))
            self.assertEqual(env["FERTILE_WEB_DATABASE"], self.target_db)
            self.assertEqual(env["FERTILE_ENABLE_WEB_NOTIFIER"], "0")
            self.assertEqual(env["FERTILE_BACKUP_REMOTE"], "")

    def test_example_stops_before_restore_on_existing_directory(self):
        self.target_data.mkdir()
        self.assertEqual(self.run_example(status=1), [])

    def test_example_stops_before_restore_on_existing_database(self):
        self.assertEqual([command for command, _, _ in self.run_example(status=1, OCCUPIED="1")], ["psql"])

    def test_example_stops_when_pre_migration_dump_is_missing(self):
        self.dump.unlink()
        self.assertEqual(self.run_example(status=1), [])

    def test_example_stops_before_launch_if_dry_run_or_restore_fails(self):
        for phase, restore_calls in (("dry-run", 1), ("restore", 2)):
            with self.subTest(phase=phase):
                trace = self.run_example(status=1, FAIL_RESTORE=phase)
                self.assertEqual([command for command, _, _ in trace], ["psql", *(["restore.sh"] * restore_calls)])
                self.trace.unlink()


if __name__ == "__main__":
    unittest.main()
