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
RESTART = "重启与外部入口"
BOOTSTRAP = "deploy/bootstrap.md"
HOT_UPGRADE = "deploy/hot-upgrade.md"
SHA = "0123456789abcdef0123456789abcdef01234567"
NORMAL_SHA = "a" * 40

OLD_RELEASE = re.compile(r"旧\s*(?:web\s*)?(?:发布|版本?|代码|制品)|上(?:一版|一个版本|一个发布)")
LIVE_DATABASE = re.compile(
    r"(?:现网|线上|生产|原(?:有)?|已迁移|迁移后|升级后|新版写过的|共享)(?:\s*(?:的|web|PostgreSQL))*\s*(?:数据库|库)"
    r"|\bweb\s*(?:数据库|库)"
)
PERMISSION = re.compile(
    r"(?<![不勿要可得能止])(?:可以|允许|仍能|仍可|照常|无需(?:恢复|还原)|可(?=在|直接|继续|用|连接)"
    r"|(?:直接|继续)(?=在|用|运行|启动|连接|使用|沿用|部署|切回))"
)
DB_ACTION = re.compile(r"(?:运行|启动|连接|使用|沿用|指向|部署|回退|切回)")


def section(document, heading):
    return re.search(rf"^##+ {re.escape(heading)}\n(.*?)(?=^##+ |\Z)",
                     (ROOT / document).read_text(), re.MULTILINE | re.DOTALL)[1]


def unsafe_migration_guidance(document):
    sentences = re.split(r"[。！？；\n]", document.replace("`", " "))
    return [line.strip() for line in sentences if "WEB_AUTO_MIGRATE" in line or all(
        pattern.search(line) for pattern in (OLD_RELEASE, LIVE_DATABASE, PERMISSION, DB_ACTION)
    )]


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

    def test_no_guide_allows_old_release_on_the_migrated_database(self):
        for document in (RUNBOOK, "docs/v1-integration.md", HOT_UPGRADE, BOOTSTRAP):
            with self.subTest(document=document):
                self.assertEqual(unsafe_migration_guidance((ROOT / document).read_text()), [])

    def test_policy_rejects_conflicting_guidance_in_reviewed_sections(self):
        for document, heading, variant in (
            (RUNBOOK, RECOVERY, "直接在原有 PostgreSQL 数据库部署旧制品"),
            (RUNBOOK, "v1 阶段 0 重新部署", "降级时允许旧版继续连接原库"),
            (HOT_UPGRADE, "应用 hook 协议 v1", "已迁移的数据库仍可用于启动旧 web 代码"),
        ):
            content = (ROOT / document).read_text()
            guide = section(document, heading)
            for conflict in ("迁移后也可以直接在现网 web 数据库运行旧发布", variant):
                with self.subTest(document=document, heading=heading, conflict=conflict):
                    contaminated = content.replace(guide, f"{guide}\n{conflict}。\n", 1)
                    self.assertIn(conflict, unsafe_migration_guidance(contaminated))
        self.assertEqual(unsafe_migration_guidance("旧代码不得连接现网 web 数据库。"), [])
        self.assertEqual(unsafe_migration_guidance("旧发布不能直接连接现网 web 数据库。"), [])

    def test_bootstrap_and_maintenance_refer_to_checked_restart(self):
        for document, heading in ((BOOTSTRAP, "重启后恢复工具链"), (RUNBOOK, "整栈维护")):
            with self.subTest(document=document):
                guide = section(document, heading)
                self.assertIn(f"#{RESTART})", guide)
                self.assertNotRegex(guide, r"scripts/fertile\s+(?:up|ensure(?:-up)?)\b")


class RollbackExampleTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.trace = self.root / "trace.jsonl"
        self.instance = self.root / "instance"
        self.rollback_record = self.instance / "migration-rollback.sha"
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
        for command in ("env", "id", "readlink"):
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
if name == "fertile" and args[:2] == ["rollback", "--switch-only"]:
    assert (Path(os.environ["FERTILE_ROOT"]) / "migration-rollback.sha").read_text() == args[2] + "\\n"
''')
        stub.chmod(0o700)
        (tools / "psql").symlink_to(stub)
        for command in ("restore.sh", "postgres.sh", "fertile"):
            (scripts / command).symlink_to(stub)

    def run_example(self, *, status, heading=RECOVERY, **env):
        block, = re.findall(r"^ *```bash\n(.*?)^ *```", section(RUNBOOK, heading), re.MULTILINE | re.DOTALL)
        result = subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-c", block], cwd=self.root,
                                env=self.env | env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, status, result.stderr)
        for path in self.protected:
            self.assertEqual(path.read_text(), "synthetic active instance marker\n")
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def link_release(self, sha):
        release = self.instance / "releases" / sha
        release.mkdir(parents=True, exist_ok=True)
        current = self.instance / "current"
        current.unlink(missing_ok=True)
        current.symlink_to(release)

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

    def test_reboot_reloads_isolated_targets_after_the_initial_shell_exits(self):
        self.run_example(status=0)
        self.assertEqual(self.rollback_record.read_text(), f"{SHA}\n")
        self.assertEqual(self.rollback_record.stat().st_mode & 0o777, 0o600)
        self.trace.unlink()
        self.target_data.mkdir()
        self.link_release(SHA)

        trace = self.run_example(status=0, heading=RESTART, old_sha="", pre_upgrade="", normal_sha=NORMAL_SHA)
        self.assertEqual([(command, args) for command, args, _ in trace], [
            ("fertile", ["ensure-up"]), ("fertile", ["status", "--json"]),
        ])
        for _, _, env in trace:
            self.assertEqual(env["FERTILE_DATA_ROOT"], str(self.target_data))
            self.assertEqual(env["FERTILE_RESEARCH_ROOT"], str(self.target_data / "research"))
            self.assertEqual(env["FERTILE_WEB_DATABASE"], self.target_db)
            self.assertEqual(env["FERTILE_ENABLE_WEB_NOTIFIER"], "0")
            self.assertEqual(env["FERTILE_BACKUP_REMOTE"], "")

    def test_reboot_refuses_old_current_without_a_rollback_record(self):
        self.link_release(SHA)
        self.assertEqual(self.run_example(status=1, heading=RESTART, normal_sha=NORMAL_SHA), [])
        self.assertEqual(self.run_example(status=1, heading=RESTART), [])

    def test_reboot_refuses_invalid_or_stale_rollback_record(self):
        self.link_release(NORMAL_SHA)
        for recorded in ("web", SHA):
            with self.subTest(recorded=recorded):
                self.rollback_record.write_text(f"{recorded}\n")
                self.target_data.mkdir(exist_ok=True)
                self.assertEqual(self.run_example(status=1, heading=RESTART, normal_sha=NORMAL_SHA), [])
        self.rollback_record.unlink()
        self.rollback_record.symlink_to(self.instance / "missing")
        self.assertEqual(self.run_example(status=1, heading=RESTART, normal_sha=NORMAL_SHA), [])

    def test_reboot_refuses_missing_or_symlinked_restored_data(self):
        self.link_release(SHA)
        self.rollback_record.write_text(f"{SHA}\n")
        self.assertEqual(self.run_example(status=1, heading=RESTART), [])
        self.target_data.symlink_to(self.instance / "data-paper")
        self.assertEqual(self.run_example(status=1, heading=RESTART), [])

    def test_reboot_uses_live_settings_only_with_the_approved_release(self):
        self.link_release(NORMAL_SHA)
        trace = self.run_example(status=0, heading=RESTART, normal_sha=NORMAL_SHA)
        self.assertEqual([(command, args) for command, args, _ in trace], [
            ("fertile", ["ensure-up"]), ("fertile", ["status", "--json"]),
        ])
        for _, _, env in trace:
            self.assertEqual(env["FERTILE_DATA_ROOT"], str(self.instance / "data-paper"))
            self.assertEqual(env["FERTILE_WEB_DATABASE"], "web")

    def test_example_stops_before_restore_on_existing_directory(self):
        self.target_data.mkdir()
        self.assertEqual(self.run_example(status=1), [])

    def test_example_preserves_an_existing_rollback_record(self):
        self.rollback_record.write_text(f"{NORMAL_SHA}\n")
        self.assertEqual(self.run_example(status=1), [])
        self.assertEqual(self.rollback_record.read_text(), f"{NORMAL_SHA}\n")

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
