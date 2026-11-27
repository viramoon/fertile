"""Exercise the actual container entrypoint and the live API handoff in a private root.

The collector generates a heartbeat and an append-only log of consecutive offsets
starting at one. It opens no feed connections. With --finance-api, the reader in
each release is the supplied application executable instead of the fixture API.
"""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import errno
import fcntl
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import resource
import shlex
import shutil
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.parse import urlsplit


REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "scripts/fertile"
BASE, TARGET = "1" * 40, "2" * 40
STREAM = "replay/synthetic"
HEARTBEAT = Path("data/replay/finance-state/collector/status.json")
WATERMARK_LOG = Path("data/replay/fixture-offsets")
MARKET = "/api/v1/market/status"
FIXTURE_FD_LIMIT = 512
OPTIONS = argparse.Namespace(require_native=False, finance_api=None, finance_config=None)


def write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def executable(path, *command):
    write(path, "#!/bin/sh\nexec " + shlex.join(map(str, command)) + ' "$@"\n')
    path.chmod(0o755)


def create_release(root, revision, *, api=None, config=None):
    release = root / "releases" / revision
    controller = release / "fertile/scripts"
    controller.mkdir(parents=True)
    for name in ("supervision.py", "handoff_start.py", "hot_upgrade.py", "backup_wal.py"):
        shutil.copyfile(REPO / "scripts" / name, controller / name)
    shutil.copytree(REPO / "deploy/supervisor", release / "fertile/deploy/supervisor")

    def row(name, role, port):
        address = "http://127.0.0.1:${" + port + "}/healthz/"
        return {
            "name": name, "role": role, "systemd": True,
            "command": ["${release}/bin/" + name],
            "directory": "${release}/finance", "limits": {"nofile": FIXTURE_FD_LIMIT},
            "live": address + "live", "ready": address + "ready",
        }

    services = [
        {**row("finance-api", "reader", "finance_port"),
         "environment": {"FINANCE_API_BIND_HOST": "127.0.0.1"}},
        {**row("finance-collector", "collector", "collector_port"),
         "source": "replay", "after": ["finance-api"]},
    ]
    write(release / "fertile/deploy/services.yaml", json.dumps({"services": services}))
    write(release / "finance/config/collector/stable.yaml", "source: replay\n")
    if config:
        for name in ("base.yaml", "stable.yaml"):
            shutil.copyfile(config / name, release / "finance/config" / name)
    else:
        api_config = {"api": {"bind_host": "127.0.0.1", "port": 1},
                      "paths": {"data_root": str(root / "data/replay"),
                                "research_root": str(root / "data/research")}}
        write(release / "finance/config/base.yaml", json.dumps(api_config))
        write(release / "finance/config/stable.yaml", "{}")

    api_path = release / "bin/finance-api"
    api_path.parent.mkdir()
    if api:
        shutil.copy2(api, api_path)
    else:
        executable(api_path, sys.executable, Path(__file__).resolve(), "_api", revision)
    executable(release / "bin/finance-collector", sys.executable, Path(__file__).resolve(), "_collector")
    for relative in ("bin/web", "bin/finance-ib", "finance/scripts/finance-backtest-worker"):
        executable(release / relative, "/bin/false")
    for relative in (
        "static/index.html", "web/config/base.yaml", "finance/sql/backtest-queue.sql", "finance/pyproject.toml",
        "finance/vendor/nautilus_trader/pyproject.toml",
        "finance/vendor/nautilus_trader/examples/backtest/example_01_load_bars_from_custom_csv/run_example.py",
    ):
        write(release / relative, "# Synthetic release fixture\n")

    # Exercise the unchanged production manifest verifier against local
    # synthetic distribution metadata. No Nautilus package is needed.
    wheel = root / "fixture.whl"
    write(wheel, "Synthetic source wheel identifier\n")
    dist_info = release / "runtime/fixture/nautilus_trader-0.0.0.dist-info"
    write(dist_info / "METADATA", "Name: nautilus_trader\nVersion: 0.0.0\n")
    write(dist_info / "direct_url.json", json.dumps({"url": wheel.as_uri()}))
    executable(release / "runtime/venv/bin/python", "/usr/bin/env",
               f"PYTHONPATH={dist_info.parent}", sys.executable)
    source = "d" * 40
    write(release / "finance/toolchain.env", f"NAUTILUS_COMMIT={source}\n")
    write(release / "finance/scripts/verify-source.py",
          "from importlib.metadata import version\n"
          "assert version('nautilus_trader') == '0.0.0'\n")
    pins = ("a" * 40, "b" * 40, "c" * 40)
    subprocess.run([sys.executable, REPO / "scripts/release-manifest.py", "create",
                    release, revision, *pins, source, wheel], check=True, timeout=20)
    subprocess.run([sys.executable, REPO / "scripts/release-manifest.py", "verify",
                    release, revision, *pins], check=True, timeout=20)
    return release


def heartbeat(root):
    return json.loads((root / HEARTBEAT).read_text())


def get_json(url):
    address = urlsplit(url)
    assert address.scheme == "http" and address.hostname == "127.0.0.1" and address.port
    with closing(HTTPConnection(address.hostname, address.port, timeout=2)) as client:
        client.request("GET", address.path)
        with client.getresponse() as response:
            assert response.status == 200
            return json.load(response)


def wait_until(check, description, *, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(.05)
    raise AssertionError(f"timed out waiting for {description}")


def synthetic_service(role, revision=None):
    root = Path(os.environ["FERTILE_ROOT"])
    stop = False

    def terminate(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)

    class FixtureHTTP(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/healthz/live", "/healthz/ready", MARKET):
                self.send_error(404)
                return
            data = heartbeat(root) if self.path == MARKET else {"pid": os.getpid(), "release": revision}
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, *_):
            pass

    assert os.environ["FERTILE_MARKET_SOURCE"] == "replay"
    assert Path(os.environ["FERTILE_DATA_ROOT"]) == root / "data/replay"
    if role == "api":
        with HTTPServer(("127.0.0.1", int(os.environ["FINANCE_API_PORT"])), FixtureHTTP) as server:
            server.timeout = .05
            while not stop:
                server.handle_request()
        return

    state_file = root / HEARTBEAT
    state_file.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    with (state_file.parent / "fixture-writer.lock").open("a") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        offsets = root / WATERMARK_LOG
        sequence = len(offsets.read_text().splitlines()) if offsets.exists() else 0
        address = urlsplit("http://" + os.environ["FINANCE_COLLECTOR_BIND"])
        assert address.hostname == "127.0.0.1"
        with offsets.open("a", buffering=1) as log, HTTPServer((address.hostname, address.port), FixtureHTTP) as server:
            server.timeout = .04
            while not stop:
                sequence += 1
                stamp = datetime.now(timezone.utc).isoformat()
                state = {"state": "ready", "source": "replay", "updated_at": stamp, "max_age_seconds": 10,
                         "instruments": [{"instrument": f"FIXTURE-{sequence}", "last_bar_at": stamp,
                                          "age_seconds": 0, "stale": False}],
                         "pid": pid, "sequence": sequence, "nofile": resource.getrlimit(resource.RLIMIT_NOFILE)[0]}
                log.write(f"{sequence} {pid}\n")
                temp = state_file.with_suffix(".tmp")
                write(temp, json.dumps(state))
                temp.replace(state_file)
                server.handle_request()
    write(root / f"collector-{pid}.stopped", str(sequence))


def market_offset(address, floor):
    def observed():
        value = get_json(address + MARKET)
        offset = int(value["instruments"][0]["instrument"].removeprefix("FIXTURE-"))
        assert value["state"] == "ready" and value["source"] == "replay"
        return offset if offset >= floor else None

    return wait_until(observed, "API to read the generated collector progress")


def confirm_api(root, revision, pid, address, native):
    if native:
        assert os.path.samefile(f"/proc/{pid}/exe", root / "releases" / revision / "bin/finance-api")
    else:
        live = get_json(address + "/healthz/live")
        assert live == {"pid": pid, "release": revision}


def application_hook(step):
    request = json.load(sys.stdin)
    root = Path(os.environ["FERTILE_ROOT"])
    context = json.loads((root / "fixture-context.json").read_text())
    assert request["service"] == "finance-api" and request["step"] == step
    if step == "compatibility":
        print(json.dumps({"compatible": True}))
        return
    before = heartbeat(root)
    assert before["pid"] == context["collector"]
    if step == "reader-checkpoint":
        print(json.dumps({"watermark": {STREAM: str(before["sequence"])}}))
        return
    assert step in {"candidate-ready", "reader-ready"}
    assert request["runtime"]["pid"] != context["old_api"]
    runtime_url = urlsplit(request["runtime"]["ready_url"])
    assert runtime_url.hostname == "127.0.0.1"
    canonical = context["canonical"]
    address = f"http://127.0.0.1:{runtime_url.port}"
    assert (address != canonical) == (step == "candidate-ready")
    confirm_api(root, request["target"], request["runtime"]["pid"], address, context["native"])
    if step == "candidate-ready":
        confirm_api(root, request["previous"], context["old_api"], canonical, context["native"])
    sequence = market_offset(address, before["sequence"] + 2)
    assert heartbeat(root)["pid"] == context["collector"]
    with (root / "fixture-phases").open("a") as log:
        log.write(json.dumps({"step": step, "collector": context["collector"], "sequence": sequence,
                              "api": request["runtime"]["pid"]}) + "\n")
    print(json.dumps({"ready": True, "watermark": {STREAM: str(sequence)}}))


def parent_pid(pid):
    fields = (Path("/proc") / str(pid) / "stat").read_text().rpartition(")")[2].split()
    return int(fields[1])


@contextmanager
def foreground_container(root, env):
    with (root / "fixture-container.log").open("w+") as log:
        container = subprocess.Popen([CLI, "container"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        try:
            yield container
        except Exception as error:
            log.seek(0)
            error.add_note(log.read())
            raise
        finally:
            if container.poll() is None:
                container.terminate()
                try:
                    container.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    # Safety net for failures in the real shutdown path. Tini,
                    # the controller, and supervisord share this fixture group.
                    os.killpg(container.pid, signal.SIGKILL)
                    container.wait(timeout=5)


def require_tools():
    tini = shutil.which("tini")
    supervisor = Path(sys.executable).parent / "supervisord"
    unavailable = []
    if not tini:
        unavailable.append("tini missing")
    if not os.access(supervisor, os.X_OK):
        unavailable.append("use the pinned Supervisor venv")
    if os.geteuid() == 0:
        unavailable.append("hot-upgrade requires an unprivileged user")
    try:
        with socket.socket(socket.AF_INET) as tcp, socket.socket(socket.AF_UNIX) as unix:
            tcp.bind(("127.0.0.1", 0))
            unix.bind("\0fer42-probe-" + str(os.getpid()))
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.EACCES):
            raise
        unavailable.append("sandbox blocks private TCP or Unix sockets")
    if unavailable:
        if OPTIONS.require_native:
            raise AssertionError(", ".join(unavailable))
        raise unittest.SkipTest(", ".join(unavailable))
    return Path(tini).resolve(), supervisor


def ephemeral_ports():
    with socket.socket() as api, socket.socket() as collector:
        for instance in (api, collector):
            instance.bind(("127.0.0.1", 0))
        return api.getsockname()[1], collector.getsockname()[1]


class ContainerHotUpgrade(unittest.TestCase):
    def test_synthetic_release_is_verified_by_the_production_manifest(self):
        with TemporaryDirectory(prefix="fer42-manifest-", dir="/tmp") as temp:
            create_release(Path(temp), BASE, api=OPTIONS.finance_api, config=OPTIONS.finance_config)

    def test_native_container_runs_real_api_handoff_without_restarting_collector(self):
        tini, supervisor = require_tools()
        with TemporaryDirectory(prefix="fer42-native-", dir="/tmp") as temp:
            root = Path(temp).resolve()
            for revision in (BASE, TARGET):
                create_release(root, revision, api=OPTIONS.finance_api, config=OPTIONS.finance_config)
            (root / "current").symlink_to(f"releases/{BASE}")
            (root / "data/replay").mkdir(parents=True)
            hook = root / "application-hook"
            executable(hook, sys.executable, Path(__file__).resolve(), "_hook")
            api_port, collector_port = ephemeral_ports()
            canonical = f"http://127.0.0.1:{api_port}"
            env = {
                "PATH": os.pathsep.join([str(tini.parent), os.defpath]),
                "PYTHONDONTWRITEBYTECODE": "1", "FERTILE_ROOT": str(root),
                "FERTILE_CONTROL_PYTHON": sys.executable, "FERTILE_SUPERVISOR_BIN": str(supervisor.parent),
                "FERTILE_MARKET_SOURCE": "replay", "FERTILE_PROFILE": "stable",
                "FINANCE_API_PORT": str(api_port), "FINANCE_COLLECTOR_PORT": str(collector_port),
            }

            def control(*args):
                result = subprocess.run([CLI, *args], env=env, check=False, capture_output=True,
                                        text=True, timeout=50)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            def running(container):
                self.assertIsNone(container.poll(), "container exited unexpectedly")
                status = control("status", "--json")
                services = status["services"]
                if all(services[name]["ready"] == 200 and services[name]["state"] == "RUNNING"
                       for name in ("finance-api", "finance-collector")):
                    return status
                return None

            with foreground_container(root, env) as container:
                first = wait_until(lambda: running(container), "the container's API and collector")
                supervisor_pid = first["supervisor_pid"]
                self.assertTrue(os.path.samefile(f"/proc/{container.pid}/exe", tini))
                controller_pid = parent_pid(supervisor_pid)
                self.assertEqual(parent_pid(controller_pid), container.pid)
                self.assertIn(b"_container", Path(f"/proc/{controller_pid}/cmdline").read_bytes().split(b"\0"))
                collector = first["services"]["finance-collector"]["pid"]
                old_api = first["services"]["finance-api"]["pid"]
                sequence = heartbeat(root)["sequence"]
                phases_path = root / "fixture-phases"
                native = bool(OPTIONS.finance_api)
                self.assertEqual(heartbeat(root)["nofile"], FIXTURE_FD_LIMIT)
                confirm_api(root, BASE, old_api, canonical, native)
                for revision, flags in ((TARGET, ()), (BASE, ("--rollback",))):
                    write(root / "fixture-context.json", json.dumps({
                        "canonical": canonical, "collector": collector, "old_api": old_api, "native": native,
                    }))
                    result = control("hot-upgrade", "finance-api", revision, "--hook", str(hook), *flags)
                    self.assertEqual((result["status"], result["target"]), ("complete", revision))
                    after = running(container)
                    self.assertIsNotNone(after)
                    self.assertEqual(after["supervisor_pid"], supervisor_pid)
                    active_api = after["services"]["finance-api"]
                    self.assertEqual(active_api["release"], str(root / "releases" / revision))
                    self.assertEqual(after["services"]["finance-collector"]["pid"], collector)
                    self.assertNotEqual(active_api["pid"], old_api)
                    candidate, canonical_stage = map(json.loads, phases_path.read_text().splitlines()[-2:])
                    self.assertEqual((candidate["step"], canonical_stage["step"]), ("candidate-ready", "reader-ready"))
                    self.assertEqual((candidate["collector"], canonical_stage["collector"]), (collector, collector))
                    self.assertEqual(canonical_stage["api"], active_api["pid"])
                    self.assertNotIn(candidate["api"], (old_api, active_api["pid"]))
                    self.assertLess(sequence, candidate["sequence"])
                    self.assertLess(candidate["sequence"], canonical_stage["sequence"])
                    self.assertEqual(result["evidence"]["reader-ready"]["watermark"][STREAM],
                                     str(canonical_stage["sequence"]))
                    sequence, old_api = canonical_stage["sequence"], active_api["pid"]
                self.assertEqual((root / "current").resolve().name, BASE)
                container.terminate()
                self.assertEqual(container.wait(timeout=20), 0)
                self.assertFalse(Path(f"/proc/{supervisor_pid}").exists())
                self.assertEqual(control("status", "--json")["supervisor"], "STOPPED")
                records = [tuple(map(int, line.split())) for line in (root / WATERMARK_LOG).read_text().splitlines()]
                self.assertGreaterEqual(len(records), sequence)
                self.assertEqual(records, [(offset, collector) for offset in range(1, len(records) + 1)])
                self.assertEqual(int((root / f"collector-{collector}.stopped").read_text()), len(records))


def main():
    global OPTIONS
    if sys.argv[1:2] in (["_api"], ["_collector"]):
        synthetic_service(sys.argv[1][1:], *sys.argv[2:])
        return
    if sys.argv[1:2] == ["_hook"]:
        application_hook(sys.argv[2])
        return
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--require-native", action="store_true")
    parser.add_argument("--finance-api", type=Path)
    parser.add_argument("--finance-config", type=Path)
    OPTIONS, rest = parser.parse_known_args()
    if OPTIONS.finance_api and not (OPTIONS.finance_api.is_file() and os.access(OPTIONS.finance_api, os.X_OK)):
        parser.error("--finance-api must name an executable file")
    unittest.main(argv=[sys.argv[0], *rest], verbosity=2)


if __name__ == "__main__":
    main()
