"""Check the release/secret boundary without touching any persistent box files."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supervision as ctl


class RuntimePlan(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = self.root / "run"
        self.logs = self.root / "logs"
        for directory in [self.run, self.logs, self.root / "config", self.root / "secrets"]:
            directory.mkdir()
        for key, value in (("ROOT", self.root), ("RUN", self.run), ("LOGS", self.logs),
                           ("CONF", self.run / "supervisord.conf"), ("SNAPSHOT", self.run / "services.json")):
            patcher = patch.object(ctl, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manifest = self.root / "services.yaml"
        env = patch.dict(os.environ, {"FERTILE_SERVICE_MANIFEST": str(self.manifest), "PATH": os.defpath}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def write(self, **extras):
        svc = {"name": "test", "directory": str(self.root), "command": ["/bin/true"], **extras}
        self.manifest.write_text(json.dumps({"services": [svc]}))

    def test_secret_referenced_by_name_and_validated_at_use(self):
        self.write(env_file="test.env")
        self.assertEqual(ctl.build_plan()["services"]["test"]["skip"], "needs_credentials")
        path = self.root / "secrets/test.env"
        path.write_text("EXAMPLE_VALUE=must-stay-out-of-rendered-files\n")
        path.chmod(0o644)
        self.assertEqual(ctl.build_plan()["services"]["test"]["skip"], "invalid_credentials_permissions")
        path.chmod(0o600)
        plan = ctl.build_plan()
        self.assertFalse(plan["services"]["test"]["skip"])
        ctl.render(plan)
        self.assertFalse(any("must-stay-out-of-rendered-files" in p.read_text()
                             for p in self.run.rglob("*") if p.is_file()))
        path.rename(path.with_suffix(".old"))
        path.symlink_to(path.with_suffix(".old"))
        self.assertEqual(ctl.build_plan()["services"]["test"]["skip"], "invalid_credentials_permissions")
        with self.assertRaises(OSError):
            ctl.read_env(path, secret=True)
        self.write(env_file="ibkr-paper.env")
        with self.assertRaisesRegex(ValueError, "invalid service env_file"):
            ctl.build_plan()

    def test_profile_mode_and_deterministic_render(self):
        self.write()
        config = self.root / "config/runtime.env"
        config.write_text("FERTILE_PROFILE=stable\nFERTILE_MARKET_SOURCE=replay\n")
        plan = ctl.build_plan()
        self.assertEqual(plan["data"], str(self.root / "data/replay"))
        env = plan["services"]["test"]["env"]
        self.assertEqual((env["FERTILE_PROFILE"], env["PYTHONDONTWRITEBYTECODE"]), ("stable", "1"))
        ctl.render(plan)
        stamp = ctl.CONF.stat().st_mtime_ns
        ctl.render(ctl.build_plan())
        self.assertEqual(stamp, ctl.CONF.stat().st_mtime_ns)
        with patch.dict(os.environ, {"FERTILE_MARKET_SOURCE":"ib"}):
            self.assertEqual(ctl.build_plan()["data"], str(self.root / "data"))
        config.write_text("FERTILE_MARKET_SOURCE=live\n")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ctl.build_plan()


if __name__ == "__main__":
    unittest.main()
