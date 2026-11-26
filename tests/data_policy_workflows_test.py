"""Exercise the checked-in workflow shell steps with offline synthetic commands."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
MARKER = "TG-PRIVATE-FIXTURE"


def step_script(workflow, job, step):
    path = ROOT / f".github/workflows/{workflow}.yml"
    steps = yaml.safe_load(path.read_text())["jobs"][job]["steps"]
    return next(item["run"] for item in steps if item.get("name") == step)


class WorkflowPolicy(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.tools = self.root / "bin"
        self.tools.mkdir()
        (self.tools / "python").symlink_to(sys.executable)
        self.env = {**os.environ, "PATH": f"{self.tools}:{os.environ['PATH']}",
                    "RUNNER_TEMP": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}

    def shell(self, script, **env):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            cwd=ROOT, env=self.env | env, text=True, capture_output=True,
        )

    def stub(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)

    def assert_private_log(self, result, status, log):
        self.assertEqual(result.returncode, status, result)
        self.assertNotIn(MARKER, result.stdout + result.stderr)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        data = log.read_text()
        self.assertIn(f"{MARKER} stdout", data)
        self.assertIn(f"{MARKER} stderr", data)
        return data

    def test_docs_artifact_gate_survives_actions_expression_expansion(self):
        script = step_script("docs-check", "docs", "检查文档与 PR 描述")
        # Actions expands expressions in the run string even inside shell/Python quotes.
        script = script.replace("${{ runner.temp }}", "/home/runner/work/_temp")
        result = self.shell(
            script, PR_BODY="Closes #41\n\n## 测试结果\n合成检查。", HEAD_REF="issue-41-data-policy-guard",
        )
        self.assertEqual(result.returncode, 0, result)
        self.assertIn("制品上传入口", result.stdout)

    def test_bootstrap_output_stays_in_runner_log_on_success_and_failure(self):
        script = step_script("bootstrap-box", "bootstrap", "首次安装、二次跳过、版本与 Postgres")
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.stub(self.tools / "runuser", f"""
printf '%s\n' '{MARKER} stdout'
printf '%s\n' '{MARKER} stderr' >&2
exit "$TG_STATUS"
""")
        for status in (0, 42):
            with self.subTest(status=status):
                result = self.shell(script, GITHUB_WORKSPACE=str(workspace), TG_STATUS=str(status))
                self.assert_private_log(result, status, self.root / "fertile-bootstrap.log")

    def test_supervisor_output_stays_in_runner_log_and_failures_stop_the_step(self):
        script = step_script("supervisor", "supervisor", "Exercise rendering, restarts and whole-stack recovery")
        self.stub(self.root / "fertile-supervisor/bin/python", f"""
printf '%s %s\n' '{MARKER} stdout' "$*"
printf '%s %s\n' '{MARKER} stderr' "$*" >&2
case "$*" in
  *"$TG_FAIL_SCRIPT") exit 43 ;;
esac
""")
        for failed in ("none", "supervision_plan.py", "supervisor_ci.py"):
            with self.subTest(failed=failed):
                result = self.shell(script, TG_FAIL_SCRIPT=failed)
                data = self.assert_private_log(
                    result, 0 if failed == "none" else 43, self.root / "fertile-supervisor.log",
                )
                self.assertIn("tests/supervision_plan.py", data)
                self.assertEqual("tests/supervisor_ci.py" in data, failed != "supervision_plan.py")


if __name__ == "__main__":
    unittest.main()
