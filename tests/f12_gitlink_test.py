"""Exercise the Integration F12 step with synthetic Git trees and a Cargo recorder."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from data_policy_workflows_test import ROOT, step_script


class F12GitlinkTests(unittest.TestCase):
    def setUp(self):
        self.stage = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        self.fertile = self.stage / "fertile"
        self.finance = self.fertile / "finance"
        self.finance.mkdir(parents=True)
        for repository in (self.fertile, self.finance):
            self.git(repository, "init", "-q")
        self.source = self.finance / "synthetic-source"
        self.source.write_text("old synthetic source\n")
        self.old = self.commit(self.finance, self.source.name, "First fixture revision")
        self.source.write_text("locked synthetic source\n")
        self.pin = self.commit(self.finance, self.source.name, "Second fixture revision")
        self.git(self.fertile, "update-index", "--add", "--cacheinfo", f"160000,{self.pin},finance")
        self.git(self.fertile, "commit", "-qm", "Lock the second fixture revision")
        self.git(self.fertile, "update-index", "--cacheinfo", f"160000,{self.old},finance")

        scripts = self.fertile / "tests"
        scripts.mkdir()
        shutil.copyfile(ROOT / "tests/finance-f12.sh", scripts / "finance-f12.sh")
        tools = self.stage / "bin"
        tools.mkdir()
        self.calls = self.stage / "cargo.jsonl"
        cargo = tools / "cargo"
        cargo.write_text("""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

record = {
    "args": sys.argv[1:],
    "source": Path("synthetic-source").read_text(),
    "cwd": str(Path.cwd()),
    "target": os.environ["CARGO_TARGET_DIR"],
    "offline": os.environ["CARGO_NET_OFFLINE"],
}
with open(os.environ["F12_CALLS"], "a", encoding="utf-8") as calls:
    calls.write(json.dumps(record) + "\\n")
""")
        cargo.chmod(0o700)
        self.env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                    "RUNNER_TEMP": str(self.stage), "F12_CALLS": str(self.calls)}

    @staticmethod
    def git(repository, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=repository, text=True,
        ).strip()

    def commit(self, repository, path, message):
        self.git(repository, "add", "--", path)
        self.git(repository, "commit", "-qm", message)
        return self.git(repository, "rev-parse", "HEAD")

    def run_step(self):
        script = step_script("integration", "integration", "Verify F12 at the locked finance gitlink")
        setup, _, commands = script.partition("\n")
        # The fixture sets its own tool environment in place of the runner setup.
        self.assertEqual(setup, 'source "$FERTILE_ROOT/env.sh"')
        return subprocess.run(["bash", "-euo", "pipefail", "-c", commands], cwd=self.fertile,
                              env=self.env, capture_output=True, text=True, timeout=20)

    def test_step_archives_the_committed_gitlink_and_runs_both_offline_tests(self):
        self.source.write_text("uncommitted synthetic source\n")
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"F12 testing finance gitlink {self.pin}", result.stdout)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["args"] for call in calls], [
            ["test", "--offline", "--locked", "--release", "-p", "finance-collector", "--test", "option_history"],
            ["test", "--offline", "--locked", "--release", "-p", "finance-ib", "--lib",
             "--features", "utoipa/chrono",
             "verified_paper_option_history_requests_and_decodes_synthetic_bars"],
        ])
        for call in calls:
            archive = Path(call["cwd"])
            self.assertEqual(archive.parent, self.stage)
            self.assertEqual(call["target"], str(archive / "target"))
            self.assertEqual(call["source"], "locked synthetic source\n")
            self.assertEqual(call["offline"], "true")
            self.assertFalse(archive.exists(), "the isolated source and target should be removed")

    def test_step_rejects_checkout_matching_only_the_staged_gitlink(self):
        self.git(self.finance, "checkout", "--quiet", "--detach", self.old)
        result = self.run_step()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"F12 finance checkout must match committed gitlink {self.pin}", result.stderr)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
