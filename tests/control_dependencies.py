"""Check the direct Python entry points without third-party site packages."""
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


REPO = Path(__file__).resolve().parents[1]


class ControlDependencies(unittest.TestCase):
    def test_missing_pyyaml_reports_how_to_run_each_entry_point(self):
        entry_points = (
            ("scripts/healthwatch.py", "scripts/healthwatch"),
            ("scripts/supervision.py", "scripts/fertile"),
            ("scripts/systemd_runtime.py", "scripts/fertile"),
            ("tests/chaos/network.py", "tests/chaos/network.sh"),
        )
        for script, wrapper in entry_points:
            with self.subTest(script=script), TemporaryDirectory() as root:
                result = subprocess.run(
                    [sys.executable, "-E", "-S", "-B", REPO / script, "--help"],
                    env={"FERTILE_ROOT": root}, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                for text in ("PyYAML is missing", wrapper, "control Python", "scripts/setup-supervisor.sh", "python3-yaml"):
                    self.assertIn(text, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(any(Path(root).iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
