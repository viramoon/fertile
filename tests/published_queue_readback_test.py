"""Check dependency preparation for the published finance queue drill offline."""

from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

import published_queue_readback as readback


class PublishedQueueDependencyTests(unittest.TestCase):
    def test_integration_prefetches_published_lock_before_offline_readback(self):
        workflow = readback.REPO / ".github/workflows/integration.yml"
        steps = yaml.safe_load(workflow.read_text())["jobs"]["integration"]["steps"]
        indices = {step.get("name"): index for index, step in enumerate(steps)}
        prepare = indices["Prepare locked test dependencies and rclone for the local encrypted remote"]
        drill = indices["Read the expanded synthetic queue with the published finance API artifact"]

        self.assertLess(prepare, drill)
        preparation = steps[prepare]["run"].replace("\\\n", "")
        self.assertIn("cargo_host=$(rustc -vV", preparation)
        calls = [shlex.split(line, comments=True) for line in preparation.splitlines()
                 if "tests/published_queue_readback.py" in line]
        self.assertIn(
            ["python3", "-B", "tests/published_queue_readback.py",
             "--finance-source", "$PWD/finance", "--fetch-published-dependencies", "$cargo_host"],
            calls,
            "the published finance lockfile must be fetched before the offline queue drill",
        )
        self.assertIn("export CARGO_NET_OFFLINE=true", steps[drill]["run"])

    def test_prefetch_archives_published_gitlink_and_uses_its_lock(self):
        pins = {"HEAD": "candidate-pin", readback.PUBLISHED_FERTILE: "published-pin"}
        target = "fixture-host"
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            source = Path(directory).resolve()
            archived = source / "published-pin"
            argv = ["published_queue_readback.py", "--finance-source", str(source),
                    "--fetch-published-dependencies", target]
            with (
                patch.object(sys, "argv", argv),
                patch.object(readback, "finance_gitlink", side_effect=pins.__getitem__),
                patch.object(readback, "archive_finance", return_value=archived) as archive,
                patch.object(readback, "build_api") as build,
                patch.object(readback.subprocess, "run") as run,
            ):
                readback.main()

            archive.assert_called_once()
            self.assertEqual(archive.call_args.args[:2], (source, "published-pin"))
            cargo_calls = [call for call in run.call_args_list if call.args[0][0] == "cargo"]
            self.assertEqual(len(cargo_calls), 1)
            self.assertEqual(cargo_calls[0].args[0], ["cargo", "fetch", "--locked", "--target", target])
            self.assertEqual(cargo_calls[0].kwargs["cwd"], archived)
            self.assertTrue(cargo_calls[0].kwargs["check"])
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
