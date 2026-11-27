"""Exercise a rendered launcher under real Supervisor without any socket listener."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supervision as ctl


# The fixture generates only process IDs, descriptor limits and a stop marker.
COLLECTOR = """\
import json
import os
from pathlib import Path
import resource
import signal
import time

root = Path(os.environ['FERTILE_ROOT'])
state = root / 'collector.json'
temp = state.with_suffix('.tmp')
def stop(*_):
    (root / 'stopped').write_text(str(os.getpid()))
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
temp.write_text(json.dumps([os.getpid(), resource.getrlimit(resource.RLIMIT_NOFILE)[0],
                            os.environ['FERTILE_MARKET_SOURCE']]))
temp.replace(state)
while True:
    time.sleep(0.1)
"""

TINI = shutil.which("tini")


class SupervisorProcess(unittest.TestCase):
    def drill(self, entrypoint):
        with TemporaryDirectory(prefix="fertile-supervisor-process.") as temp:
            root = Path(temp)
            run = root / "run"
            logs = root / "logs"
            for directory in (run, logs, root / "config"):
                directory.mkdir()
            launcher = root / "collector.py"
            launcher.write_text(COLLECTOR)
            manifest = root / "services.yaml"
            manifest.write_text(json.dumps({"services": [{
                "name": "fixture-collector", "command": [sys.executable, str(launcher)],
                "directory": str(root), "limits": {"nofile": 512},
            }]}))
            environment = {
                "FERTILE_ROOT": str(root), "PATH": os.defpath,
                "FERTILE_MARKET_SOURCE": "replay", "FERTILE_SERVICE_MANIFEST": str(manifest),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            config = run / "supervisord.conf"
            with (patch.dict(os.environ, environment, clear=True),
                  patch.multiple(ctl, ROOT=root, RUN=run, LOGS=logs, CONF=config,
                                 SNAPSHOT=run / "services.json")):
                ctl.render(ctl.build_plan())

            # The fixture drives startup through the config and observes state files.
            # The generated command, restart policy, logs and signal handling run intact.
            configuration = config.read_text()
            config.write_text(configuration[configuration.index("[supervisord]"):
                                            configuration.index("[rpcinterface:supervisor]")]
                              + configuration[configuration.index("[include]"):])
            programs = run / "programs.conf"
            programs.write_text(programs.read_text().replace("autostart=false", "autostart=true"))
            if "PYTHONPATH" in os.environ:
                environment["PYTHONPATH"] = os.environ["PYTHONPATH"]

            command = [*entrypoint, sys.executable, "-B", "-m", "supervisor.supervisord", "-n", "-c", config]
            with subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) as supervisor:
                log = logs / "supervisord.log"

                def running(number):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and supervisor.poll() is None:
                        if log.exists() and log.read_text().count("fixture-collector entered RUNNING state") >= number:
                            return json.loads((root / "collector.json").read_text())
                        time.sleep(0.05)
                    self.fail(log.read_text() if log.exists() else "supervisord exited before logging")

                try:
                    first, limit, source = running(1)
                    self.assertEqual((limit, source), (512, "replay"))
                    os.kill(first, signal.SIGKILL)
                    second, limit, source = running(2)
                    self.assertNotEqual(first, second)
                    self.assertEqual((limit, source), (512, "replay"))
                    supervisor.terminate()
                    self.assertEqual(supervisor.wait(timeout=8), 0)
                    self.assertEqual((root / "stopped").read_text(), str(second))
                finally:
                    if supervisor.poll() is None:
                        supervisor.terminate()
                        try:
                            supervisor.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            supervisor.kill()
                            supervisor.wait(timeout=2)

    def test_rendered_program_restarts_after_kill_and_stops_cleanly(self):
        self.drill([])

    @unittest.skipUnless(TINI, "tini executable is required for the container drill")
    def test_tini_forwards_term_to_foreground_supervisor(self):
        self.drill([TINI, "-s", "--"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
