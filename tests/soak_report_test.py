"""Exercise the report CLI against small on-disk healthwatch histories."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPORT = Path(__file__).resolve().parents[1] / "scripts" / "soak_report.py"
EPOCH = datetime(2025, 11, 1, tzinfo=timezone.utc)


def at(offset):
    return (EPOCH + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


class SoakReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.samples = self.root / "samples.jsonl"
        self.alerts = self.root / "alerts.jsonl"
        self.output = self.root / "reports" / "soak.md"
        self.samples.touch()
        self.alerts.touch()

    def sample(self, offset, state="ok", *, interval=30, market_age=None, include_ready=True):
        checks = {"public:ready": {"state": "skipped", "detail": "公网域名尚未确认"}}
        if include_ready:
            checks["service:web:ready"] = {"state": state}
        if market_age is not None:
            checks["market:fresh"] = {"state": "ok", "max_age_seconds": market_age}
        with self.samples.open("a") as handle:
            handle.write(json.dumps({"timestamp": at(offset), "interval_seconds": interval,
                                     "checks": checks}) + "\n")

    def run_report(self, *args):
        return subprocess.run([sys.executable, REPORT, "--samples", self.samples, "--alerts",
                               self.alerts, "--output", self.output, "--label", "测试夹具", *args],
                              capture_output=True, text=True, check=False)

    def test_short_window_reports_actual_health_and_only_real_alerts(self):
        self.sample(0, market_age=4)
        self.sample(30, "failed", market_age=34)
        self.sample(60, market_age=3)
        self.alerts.write_text(json.dumps({"timestamp": at(31), "check": "service:web:ready",
                                          "event": "failure", "detail": "HTTP 503 | failed"}) + "\n")
        self.assertEqual(self.run_report().returncode, 0)
        report = self.output.read_text()
        self.assertIn("连续 24 小时采样门禁：未达到", report)
        self.assertIn("| service:web:ready | 2 | 1 | 0 | 0 | 66.67% |", report)
        self.assertIn("| public:ready | 0 | 0 | 3 | 0 | 无实测 | 公网域名尚未确认 |", report)
        self.assertIn("最大实测行情年龄 34 秒", report)
        self.assertIn("| 已恢复 | 30 |", report)
        self.assertIn(r"HTTP 503 \| failed", report)
        self.assertEqual(self.run_report("--require-hours", "24").returncode, 1)

    def test_gaps_missing_checks_and_uncovered_window_edges_fail_gate(self):
        self.sample(30, "failed")
        self.sample(60, include_ready=False)
        self.sample(150, "failed")
        self.sample(180)
        result = self.run_report("--start", at(0), "--end", at(210), "--require-hours", "0.001")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = self.output.read_text()
        self.assertIn("相邻实测样本覆盖 60 秒，窗口共 210 秒。缺口 3 段，共 150 秒", report)
        self.assertIn("| service:web:ready | 1 | 2 | 0 | 1 | 33.33% |", report)
        self.assertIn("观测中断，恢复未知", report)
        self.assertIn("| 已恢复 | 30 |", report)
        self.assertNotIn("| 已恢复 | 150 |", report)

    def test_outage_carries_into_window_without_looking_past_its_end(self):
        for offset, state in ((0, "ok"), (30, "failed"), (60, "failed"),
                              (90, "failed"), (120, "ok")):
            self.sample(offset, state)
        args = ["--start", at(60), "--end", at(90)]
        self.assertEqual(self.run_report(*args).returncode, 0)
        report = self.output.read_text()
        self.assertIn(at(30) + "（窗前已失败）", report)
        self.assertIn("末次采样仍失败", report)
        self.assertNotIn(at(120), report)
        self.assertEqual(self.run_report("--start", at(65), "--end", at(120)).returncode, 0)
        self.assertIn("| 已恢复 | 90 |", self.output.read_text())

    def test_full_24h_window_and_gap_of_one_missing_sample(self):
        # Fixture uses real scheduled instants. Each completed pair contributes 30 s.
        for offset in range(0, 86401, 30):
            self.sample(offset)
        self.assertEqual(self.run_report("--require-hours", "24").returncode, 0)
        self.assertIn("相邻实测样本覆盖 86400 秒", self.output.read_text())
        rows = self.samples.read_text().splitlines()
        del rows[100]
        self.samples.write_text("\n".join(rows) + "\n")
        self.assertEqual(self.run_report("--require-hours", "24").returncode, 1)
        self.assertIn("缺口 1 段，共 60 秒", self.output.read_text())

    def test_previous_check_absent_for_entire_selected_window_counts_as_missing(self):
        self.sample(0, market_age=3)
        self.sample(30)
        self.sample(60)
        result = self.run_report("--start", at(30), "--end", at(60), "--require-hours", "0.001")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("| market:fresh | 0 | 0 | 0 | 2 | 无实测 |", self.output.read_text())

    def test_bad_evidence_does_not_replace_existing_report(self):
        self.sample(1)
        self.assertEqual(self.run_report().returncode, 0)
        original = self.output.read_bytes()
        for content in ("{", json.dumps({"timestamp": "2100-01-01T00:00:00Z"}),
                        self.samples.read_text() * 2):
            self.samples.write_text(content)
            self.assertEqual(self.run_report().returncode, 2)
            self.assertEqual(original, self.output.read_bytes())
        self.samples.write_text("")
        self.assertEqual(self.run_report("--start", at(0), "--end", at(3600),
                                         "--require-hours", "1").returncode, 1)
        self.assertIn("窗内样本：0", self.output.read_text())


if __name__ == "__main__":
    unittest.main()
