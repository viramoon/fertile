"""Exercise the real fertile control plane in a temporary supervisor instance (Linux)."""
import configparser
import glob
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time

from supervisor_drill import children


TESTS = Path(__file__).resolve().parent
CLI = TESTS.parent / "scripts/fertile"


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path in ("/healthz/live", "/healthz/ready") else 404)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, *_args):
        pass


def fixture(root, name, port):
    stop = False

    def terminate(_signal, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, terminate)
    marker = root / "events" / f"{name}.{os.getpid()}"
    marker.touch()
    # PostgreSQL and workers may spawn descendants. Exercise stopasgroup and
    # whole-tree reboot behavior with a second, independently observable process.
    child = subprocess.Popen([sys.executable, __file__, "fixture", str(root), "child", "0"]) if name == "postgres" else None
    server = HTTPServer(("127.0.0.1", port), Health) if port else None
    if server:
        server.timeout = .05
    try:
        while not stop:
            if server:
                server.handle_request()
            else:
                time.sleep(.05)
    finally:
        if server:
            server.server_close()
        if child:
            child.wait(timeout=4)  # Only the real supervisor process-group signal stops this child.
        marker.with_suffix(marker.suffix + ".term").touch()


def port_number():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def check_render(root):
    parser = configparser.RawConfigParser(interpolation=None)
    main = root / "run/supervisord.conf"
    parser.read(main)
    for pattern in parser["include"]["files"].split():
        parser.read(glob.glob(str(main.parent / pattern)))
    enabled = [s.removeprefix("program:") for s in parser.sections() if s.startswith("program:")]
    assert sorted(enabled) == ["finance-api", "postgres"], enabled
    assert parser["unix_http_server"]["file"] == str(root / "run/supervisor.sock")
    assert parser["supervisord"]["pidfile"] == str(root / "run/supervisord.pid")
    assert (root / "run/supervisor.sock").stat().st_mode & 0o777 == 0o600
    for name in enabled:
        section = parser["program:" + name]
        assert section.getboolean("autorestart") and not section.getboolean("autostart")
        assert section.getint("startretries") > 0 and section.getint("startsecs") > 0
        assert section["stopsignal"] == "TERM" and section.getint("stopwaitsecs") == 30
        assert section.getboolean("stopasgroup") and section.getboolean("killasgroup")
        assert section["stdout_logfile_maxbytes"].upper() == "50MB" and section.getint("stdout_logfile_backups") == 10
        assert Path(section["stdout_logfile"]).parent == root / "logs" / name
    assert parser["program:postgres"].getint("priority") < parser["program:finance-api"].getint("priority")
    print("Rendered priority, socket permissions, restart, shutdown groups and 50MB x 10 rotation verified", flush=True)


def run(root):
    def fixture_service(name, port=0, enabled=True):
        service = {"name": name, "enabled": enabled, "directory": str(root),
                   "command": [sys.executable, __file__, "fixture", str(root), name, str(port)]}
        if port:
            service.update(live=f"http://127.0.0.1:{port}/healthz/live", ready=f"http://127.0.0.1:{port}/healthz/ready")
        return service

    (root / "events").mkdir()
    manifest = root / "services.yaml"
    manifest.write_text(json.dumps({"services": [
        fixture_service("postgres"),
        fixture_service("finance-api", port_number()),
        fixture_service("web", enabled=False),
        {"name": "absent-tool", "enabled": True, "command": [str(root / "absent-executable")], "directory": str(root)},
    ]}))
    env = {**os.environ, "FERTILE_ROOT": str(root), "FERTILE_BOX_HOME": str(root / "box"),
           "FERTILE_CONTROL_PYTHON": sys.executable, "FERTILE_SUPERVISOR_BIN": str(Path(sys.executable).parent),
           "FERTILE_SERVICE_MANIFEST": str(manifest), "PYTHONDONTWRITEBYTECODE": "1"}
    def control(*args):
        subprocess.run([CLI, *args], check=True, env=env, timeout=75)
    def status():
        return json.loads(subprocess.check_output([CLI, "status", "--json"], env=env, timeout=8))
    version = subprocess.check_output([Path(sys.executable).parent / "supervisord", "--version"], text=True).strip()
    assert version == "4.3.0", version
    print("Real supervisord", version, "isolated root", root, flush=True)
    try:
        control("up")
        first = status()
        check_render(root)
        control("up")
        second = status()
        assert first["supervisor_pid"] == second["supervisor_pid"] > 1
        for name in ("postgres", "finance-api"):
            assert first["services"][name]["pid"] == second["services"][name]["pid"] > 1
        assert first["services"]["web"]["state"] == second["services"]["web"]["state"] == "disabled"
        assert first["services"]["absent-tool"]["state"] == second["services"]["absent-tool"]["state"] == "missing_binary"
        assert second["services"]["finance-api"]["ready"] == 200
        print("Two up calls retained daemon and both program PIDs:", second["supervisor_pid"],
              {k: v["pid"] for k, v in second["services"].items()}, flush=True)
        for script in ("chaos-restart.sh", "simulate-reboot.sh"):
            subprocess.run([TESTS / script], env=env, check=True, timeout=85)

        final = status()
        assert final["services"]["web"]["state"] == "disabled" and final["services"]["absent-tool"]["state"] == "missing_binary"
        assert not list((root / "events").glob("web.*"))
        current = {name: final["services"][name]["pid"] for name in ("finance-api", "postgres")}
        # Only count the current child actually spawned by our current postgres fixture.
        child_pids = children(current["postgres"])
        assert len(child_pids) == 1, child_pids
        current["child"] = child_pids.pop()
        assert all((root / "events" / f"{name}.{pid}").is_file() for name, pid in current.items())
        pids = [final["supervisor_pid"], *current.values()]
        handles = [os.pidfd_open(pid) for pid in pids]
        try:
            control("down")
            deadline = time.monotonic() + 5
            for pid, fd in zip(pids, handles):
                assert select.select([fd], [], [], max(0, deadline - time.monotonic()))[0], f"PID {pid} survived down"
        finally:
            for fd in handles:
                os.close(fd)
        assert status()["supervisor"] == "STOPPED"
        assert all((root / "events" / f"{name}.{pid}.term").is_file() for name, pid in current.items())
        print("down returned after TERM stopped both programs and their child; old daemon/service/child PIDs exited:", pids, flush=True)
        control("down")  # also idempotent after shutdown
    finally:
        # The manifest and all commands are confined to this temporary root.
        control("down")


if __name__ == "__main__":
    if sys.argv[1:2] == ["fixture"]:
        fixture(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
    else:
        # A short private path also works with the kernel limit on Unix socket path length.
        with tempfile.TemporaryDirectory(prefix="fertile-ci-", dir="/tmp") as work:
            run(Path(work))
