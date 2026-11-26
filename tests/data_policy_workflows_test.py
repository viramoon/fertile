"""Exercise the checked-in workflow shell steps with offline synthetic commands."""
import hashlib
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


class IntegrationDownloads(unittest.TestCase):
    payload = b"synthetic OpenAPI Generator download fixture\n"
    url = "https://downloads.example.invalid/openapi-generator-cli-1.2.3.jar"

    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        tools = self.root / "bin"
        tools.mkdir()
        generator = self.root / "web/scripts/gen-finance-client.sh"
        generator.parent.mkdir(parents=True)
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        generator.write_text(
            f'version=1.2.3\nchecksum={self.sha256}\n'
            '    "https://downloads.example.invalid/openapi-generator-cli-$version.jar" -o "$download"\n'
            'touch sourced-generator\n'
        )
        (self.root / "scripts").symlink_to(ROOT / "scripts")
        (self.root / "payload").write_bytes(self.payload)
        self.output = self.root / "outputs"
        self.jar = self.root / "openapi-generator-cli-1.2.3.jar"
        self.env = {
            "PATH": f"{tools}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.root),
            "GITHUB_OUTPUT": str(self.output),
        }
        curl = tools / "curl"
        curl.write_text(f"#!{sys.executable}\n" + """
import os
from pathlib import Path
import sys

root = Path(os.environ["RUNNER_TEMP"])
args = sys.argv[1:]
partial = Path(args[args.index("--output") + 1])
assert args == [
    "--disable", "--fail", "--location", "--silent", "--proto", "=https",
    "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "60",
    "--output", str(partial), "--write-out", "%{http_code}", "--", os.environ["OPENAPI_GENERATOR_URL"],
], args
jar = Path(os.environ["OPENAPI_GENERATOR_JAR"])
assert partial.parent == jar.parent and partial.name.startswith(jar.name + ".part.")
assert not jar.exists()
trace = root / "trace"
attempt = trace.read_text().splitlines().count("curl") if trace.exists() else 0
with trace.open("a") as file:
    file.write("curl\\n")
status, http = os.environ["CURL_RESULTS"].split()[attempt].split(":")
partial.write_bytes((root / "payload").read_bytes() if status == "0" else b"synthetic error body")
print(http, end="")
sys.exit(int(status))
""")
        curl.chmod(0o700)
        sleep = tools / "sleep"
        sleep.write_text('#!/bin/sh\nprintf "sleep %s\\n" "$1" >> "$RUNNER_TEMP/trace"\n')
        sleep.chmod(0o700)

    def shell(self, script, **env):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            cwd=self.root, env=self.env | env, text=True, capture_output=True, timeout=10,
        )

    def download(self, cache_hit, results=""):
        result = self.shell(
            step_script("integration", "integration", "Verify or download the pinned OpenAPI Generator JAR"),
            CACHE_HIT=cache_hit, CURL_RESULTS=results, OPENAPI_GENERATOR_URL=self.url,
            OPENAPI_GENERATOR_SHA256=self.sha256, OPENAPI_GENERATOR_JAR=str(self.jar),
        )
        self.assertNotIn("synthetic error body", result.stdout + result.stderr)
        self.assertEqual(list(self.root.glob("*.part.*")), [])
        return result

    def assert_trace(self, *expected):
        trace = self.root / "trace"
        self.assertEqual(trace.read_text().splitlines() if trace.exists() else [], list(expected))

    def test_cache_structure_uses_exact_version_digest_and_single_jar(self):
        steps = yaml.safe_load((ROOT / ".github/workflows/integration.yml").read_text())["jobs"]["integration"]["steps"]
        pin = next(i for i, step in enumerate(steps) if step.get("id") == "openapi-pin")
        _, restore, verify, save, contract = steps[pin:pin + 5]
        jar = "${{ steps.openapi-pin.outputs.jar }}"
        cache_inputs = {
            "path": jar,
            "key": "fertile-openapi-generator-${{ steps.openapi-pin.outputs.version }}-${{ steps.openapi-pin.outputs.sha256 }}",
        }
        self.assertEqual(restore["uses"], "actions/cache/restore@v4")
        self.assertEqual(restore["with"], cache_inputs)
        self.assertEqual(verify["env"], {
            "CACHE_HIT": "${{ steps.restore-openapi.outputs.cache-hit }}",
            "OPENAPI_GENERATOR_JAR": jar,
            "OPENAPI_GENERATOR_SHA256": "${{ steps.openapi-pin.outputs.sha256 }}",
            "OPENAPI_GENERATOR_URL": "${{ steps.openapi-pin.outputs.url }}",
        })
        self.assertNotIn("if", verify)
        self.assertFalse(verify.get("continue-on-error"))
        self.assertEqual(save["uses"], "actions/cache/save@v4")
        self.assertEqual(save["with"], cache_inputs)
        self.assertEqual(save["if"], "steps.restore-openapi.outputs.cache-hit != 'true'")
        self.assertEqual(contract["env"]["OPENAPI_GENERATOR_JAR"], jar)
        self.assertIn("scripts/contract-diff.sh", contract["run"])
        self.assertIn("bash tests/contract-drift.sh", contract["run"])

    def test_pin_reads_web_version_digest_and_url_without_executing_script(self):
        result = self.shell(step_script("integration", "integration", "Read the OpenAPI Generator pin from web"))
        self.assertEqual(result.returncode, 0, result)
        self.assertFalse((self.root / "sourced-generator").exists())
        self.assertEqual(dict(line.split("=", 1) for line in self.output.read_text().splitlines()), {
            "version": "1.2.3", "sha256": self.sha256, "url": self.url, "jar": str(self.jar),
        })

    def test_invalid_pin_fails_before_cache_restore(self):
        with (self.root / "web/scripts/gen-finance-client.sh").open("a") as generator:
            generator.write("checksum=invalid\n")
        result = self.shell(step_script("integration", "integration", "Read the OpenAPI Generator pin from web"))
        self.assertNotEqual(result.returncode, 0, result)
        self.assertIn("Cannot read the pinned OpenAPI Generator", result.stdout)
        self.assertFalse(self.output.exists())

    def test_cache_hit_checks_bytes_without_request(self):
        self.jar.write_bytes(self.payload)
        result = self.download("true")
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(self.jar.read_bytes(), self.payload)
        self.assert_trace()

    def test_cache_hit_with_missing_or_corrupt_jar_fails_without_request(self):
        for content, error in [(None, "Restored file is missing"), (b"corrupt", "Restored SHA-256 mismatch")]:
            with self.subTest(error=error):
                if content is not None:
                    self.jar.write_bytes(content)
                result = self.download("true")
                self.assertNotEqual(result.returncode, 0, result)
                self.assertIn(error, result.stderr)
                self.assert_trace()

    def test_cache_miss_retries_429_and_publishes_verified_jar(self):
        result = self.download("", "22:429 22:429 0:200")
        self.assertEqual(result.returncode, 0, result)
        self.assertIn("HTTP 429", result.stderr)
        self.assertEqual(self.jar.read_bytes(), self.payload)
        self.assert_trace("curl", "sleep 1", "curl", "sleep 2", "curl")

    def test_cache_miss_stops_after_four_attempts(self):
        result = self.download("false", "22:429 22:429 28:000 22:503")
        self.assertNotEqual(result.returncode, 0, result)
        self.assertIn("Download failed after 4 attempts (HTTP 503", result.stderr)
        self.assertFalse(self.jar.exists())
        self.assert_trace("curl", "sleep 1", "curl", "sleep 2", "curl", "sleep 4", "curl")

    def test_cache_miss_fails_fast_for_permanent_http_error(self):
        result = self.download("false", "22:404")
        self.assertNotEqual(result.returncode, 0, result)
        self.assertIn("Download failed (HTTP 404", result.stderr)
        self.assertFalse(self.jar.exists())
        self.assert_trace("curl")

    def test_cache_miss_rejects_unverified_bytes(self):
        (self.root / "payload").write_bytes(b"corrupt")
        result = self.download("false", "0:200")
        self.assertNotEqual(result.returncode, 0, result)
        self.assertIn("Downloaded SHA-256 mismatch", result.stderr)
        self.assertFalse(self.jar.exists())
        self.assert_trace("curl")


if __name__ == "__main__":
    unittest.main()
