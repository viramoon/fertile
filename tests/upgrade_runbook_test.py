"""Check documented upgrade gates with a synthetic local SQLite queue."""

import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rollback_docs_test import ROOT, RUNBOOK, section


WS = "websocket-入口核对"
MANUAL = "受监管启停与手动排空"
RECOVERY = "从升级前快照回滚并切回新版"
INTEGRATION = "docs/v1-integration.md"
HOT_UPGRADE = "deploy/hot-upgrade.md"
LINUX_SERVER = "deploy/linux-server.md"


class UpgradeRunbookTests(unittest.TestCase):
    def test_websocket_checks_follow_the_actual_request_path(self):
        guide = section(RUNBOOK, "WebSocket 入口核对")
        topology, proxy, direct = guide.split("- **")
        for clue in ("路由", "监听地址", "127.0.0.1:8090", "中途无 HTTP 反向代理"):
            self.assertIn(clue, topology)
        self.assertIn("前置反向代理时**，才核对", proxy)
        for header in ("Upgrade: websocket", "Connection: Upgrade", "原样传递浏览器 `Origin`"):
            self.assertIn(header, proxy)
        self.assertIn("直连回环端口时**，跳过代理升级头和 `Origin` 转发核对", direct)
        for check in ("/healthz/ready", "/api/v1/system/status", "Bearer", "应用层 `Origin` 校验"):
            self.assertIn(check, direct)
        for path in (proxy, direct):
            for check in ("/api/v1/stream/market", "101 Switching Protocols", "市场帧", "重连"):
                self.assertIn(check, path)

    def test_manual_drain_precedes_managed_stop_and_start(self):
        steps = re.split(r"(?m)^\d+\. ", section(RUNBOOK, MANUAL))[1:]
        prepare, drain, stop, start, verify, failure = steps
        for check in ("维护窗口", "排空截止时间", "--stage-only", "可恢复备份", "未完成的交接事务"):
            self.assertIn(check, prepare)
        for check in ("新回测提交", "直连 finance API", "在途提交均已结算", "保持旧 worker 运行",
                      "租约均为 `0`", "没有在途引擎子进程", "超时或有孤立作业、租约时暂停换版"):
            self.assertIn(check, drain)
        for worker, stack in (("fertile stop finance-backtest-worker", "fertile down"),
                              ("systemctl stop fertile-finance-backtest-worker.service", "systemctl stop fertile.target")):
            with self.subTest(worker=worker):
                self.assertLess(stop.index(worker), stop.index(stack))
        self.assertIn("写者锁及源订阅已释放", stop)
        self.assertIn("IBKR 继续使用 paper 只读", stop)
        self.assertLess(start.index("迁移前快照"), start.index("web migrate"))
        for check in ("SQLx 台账", "#重启与外部入口", "systemctl start fertile.target"):
            self.assertIn(check, start)
        for check in ("HTTP 就绪", "原持久队列", WS, "缺口", "再恢复提交与调度"):
            self.assertIn(check, verify)
        self.assertIn("保持维护状态", failure)

    def test_explicit_migration_gates_both_managed_start_paths(self):
        start = re.split(r"(?m)^\d+\. ", section(RUNBOOK, MANUAL))[4]
        self.assertRegex(start, r"在启动新版业务进程前[^。]*`web migrate`[^。]*SQLx 台账")
        self.assertIn("只运行 PostgreSQL，保持 finance API、collector、worker、web 和 notifier 停止", start)
        self.assertIn("迁移或台账核对失败时保持业务进程停止", start)
        self.assertIn("systemd 的 web `ExecStartPre` 都在 finance 服务启动阶段之后", start)
        order = ("迁移前快照", "受管 PostgreSQL", "`fertile/scripts/postgres.sh foreground`", "`FERTILE_WEB_PEER=1`",
                 "外部 PostgreSQL", '`"$FERTILE_ROOT/current/bin/web" migrate`', "核对退出码与该库台账",
                 "迁移及台账核对通过后", "`scripts/postgres.sh stop`", "#重启与外部入口",
                 "`sudo systemctl start fertile.target`")
        offsets = [start.index(check) for check in order]
        self.assertEqual(offsets, sorted(offsets))

    def test_systemd_version_switch_drains_before_stopping_units(self):
        guide = section(LINUX_SERVER, "systemd")
        upgrade = next(paragraph for paragraph in guide.split("\n\n") if "整体换版" in paragraph)
        order = ("暂停提交", "确认 worker 已排空", "`sudo systemctl stop fertile-finance-backtest-worker.service`",
                 "确认 worker 和引擎均已退出", "`sudo systemctl stop fertile.target`",
                 "在启动 target 前单独完成 `web migrate` 并核对 SQLx 台账", "`install-systemd`",
                 "`sudo systemctl start fertile.target`")
        offsets = [upgrade.index(check) for check in order]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn(f"../{RUNBOOK}#{MANUAL})", upgrade)

    def test_drain_query_distinguishes_queued_running_and_leases(self):
        query = re.search(r"(?m)^   ```sql\n([\s\S]+?)^   ```", section(RUNBOOK, MANUAL))[1]
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "jobs.sqlite3"
            with closing(sqlite3.connect(database)) as fixture:
                # Deterministic rows cover every state relevant to the drain gate.
                fixture.executescript("""
                    CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
                    CREATE TABLE job_leases (job_id INTEGER PRIMARY KEY);
                """)
                with fixture:
                    fixture.executemany("INSERT INTO jobs VALUES (?, ?)",
                                        [(1, "queued"), (2, "running"), (3, "succeeded")])
                    fixture.execute("INSERT INTO job_leases VALUES (?)", (2,))
                with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as reader:
                    def counts():
                        result = reader.execute(query)
                        self.assertEqual([column[0] for column in result.description],
                                         ["queued", "running", "leases"])
                        return result.fetchone()

                    self.assertEqual(counts(), (1, 1, 1))
                    with fixture:
                        fixture.execute("UPDATE jobs SET status = 'succeeded' WHERE id = 1")
                    self.assertEqual(counts(), (0, 1, 1))
                    with fixture:
                        fixture.execute("UPDATE jobs SET status = 'failed' WHERE id = 2")
                    self.assertEqual(counts(), (0, 0, 1))
                    with fixture:
                        fixture.execute("DELETE FROM job_leases")
                    self.assertEqual(counts(), (0, 0, 0))

    def test_legacy_queue_drains_without_a_lease_table(self):
        guide = section(RUNBOOK, MANUAL)
        documented = re.search(r"确认旧发布未采用 F13 且无 `job_leases` 表时[^\n]+"
                               r"\n\n   ```sql\n(.*?)\n   ```", guide, re.DOTALL)
        self.assertIsNotNone(documented, "legacy queues need their own read-only drain query")
        self.assertIn("两个计数均为 `0` 且没有在途引擎子进程", documented[0])
        self.assertIn("若旧制品应有租约表但实际缺失，保持维护状态", documented[0])

        for status, counts in (("queued", (1, 0)), ("running", (0, 1)), ("succeeded", (0, 0))):
            with self.subTest(status=status), closing(sqlite3.connect(":memory:")) as db:
                db.execute("CREATE TABLE jobs (status TEXT NOT NULL)")
                db.execute("INSERT INTO jobs VALUES (?)", (status,))
                db.execute("PRAGMA query_only = ON")
                result = db.execute(documented[1])
                self.assertEqual([column[0] for column in result.description], ["queued", "running"])
                self.assertEqual(result.fetchone(), counts)

    def test_manual_path_uses_t5_isolated_rollback(self):
        guide = section(RUNBOOK, MANUAL)
        self.assertIn(f"#{RECOVERY})", guide)
        self.assertRegex(guide, r"迁移前备份恢复到新建的 `data-paper-rollback-<sha>` 和 `web_rollback_<sha>`，"
                                r"再在这对目标上运行旧发布")
        self.assertIn("旧代码不得连接新版写过的数据库", guide)
        self.assertRegex(guide, r"原目标上的回退只在无新增迁移[^。]*兼容[^。]*未被新版写过时适用")

    def test_upgrade_and_ws_guides_link_to_the_runbook(self):
        for document in (INTEGRATION, HOT_UPGRADE):
            with self.subTest(document=document):
                content = (ROOT / document).read_text()
                for anchor in (WS, MANUAL, RECOVERY):
                    self.assertIn(f"../{RUNBOOK}#{anchor})", content)
                self.assertIn("前置反向代理", content)
                self.assertIn("直连", content)
        readme = (ROOT / "README.md").read_text()
        self.assertIn(f"{RUNBOOK}#{MANUAL})", readme)
        self.assertIn("deploy/hot-upgrade.md)", readme)

    def test_notifier_disabled_documents_manager_and_web_behavior(self):
        services = json.loads((ROOT / "deploy/services.yaml").read_text())["services"]
        notifier = next(service for service in services if service["name"] == "web-notifier")
        setting = f'`{notifier["enable_setting"]}=0`'
        for document in (RUNBOOK, INTEGRATION, HOT_UPGRADE):
            with self.subTest(document=document):
                content = (ROOT / document).read_text()
                for check in (f"{setting} 时守护器不启动 `web-notifier`", "`disabled`", "`0`", "W11", "安全空转"):
                    self.assertIn(check, content)
        guide = section(RUNBOOK, "v1 T3a 首批运行时核对")
        self.assertIn("保留 outbox 且不发送邮件", guide)
        self.assertRegex(guide, r"https://github\.com/viramoon/web/blob/[0-9a-f]{40}/RUNBOOK\.md#告警与邮件进程")


if __name__ == "__main__":
    unittest.main()
