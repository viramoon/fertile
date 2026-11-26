"""Connect the journaled handoff to the installed process manager."""
import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from xmlrpc.client import Fault

import hot_upgrade
import handoff_start
import service_releases
import supervision as ctl
import systemd_units as units


READER_ENV = {
    "finance-api": ("FINANCE_API_PORT", "FINANCE_API_BIND_HOST"),
    "web": ("WEB_PORT", "WEB_BIND_HOST"),
}
TOPOLOGY = ("role", "source", "systemd", "after", "limits", "startup", "live_url", "ready_url")
INSTANCE_ENV = {
    "FERTILE_DATA_ROOT", "FERTILE_RESEARCH_ROOT", "FERTILE_PROFILE", "FERTILE_MARKET_SOURCE",
    "PGDATABASE", "PGHOST", "PGPORT", "FINANCE_BASE_URL", "FINANCE_API_PORT", "FINANCE_COLLECTOR_BIND", "WEB_PORT",
}
HOOK_RESPONSE_LIMIT = 64 * 1024
HOOK_TIMEOUT = 120


def verify_release(release):
    recorded = json.loads((release / "manifest.json").read_text())
    components = recorded["components"]
    subprocess.run([sys.executable, str(ctl.SCRIPTS / "release-manifest.py"), "verify",
                    str(release), release.name, *(components[key] for key in ("finance", "web", "frontend"))],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hook_command(value):
    hook = Path(value)
    if (not hook.is_absolute() or hook.resolve() != hook or not hook.is_file()
            or not os.access(hook, os.X_OK) or hook.stat().st_mode & 0o022):
        raise ValueError("hook must be an absolute executable file without symbolic links or shared write permission")

    def invoke(step, record):
        env = {key: os.environ[key] for key in ctl.INHERIT if key in os.environ}
        env["FERTILE_ROOT"] = str(ctl.ROOT)
        command = [sys.executable, "-B", hot_upgrade.__file__, str(os.getpid()), str(hook), step]
        # The hook can begin writing before reading all the request. Provide
        # stdin as a private file so stdout can be drained with a byte bound.
        with tempfile.TemporaryFile() as request:
            request.write(json.dumps({**record, "step": step}).encode("utf-8"))
            request.seek(0)
            with subprocess.Popen(command, stdin=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=env, start_new_session=True) as process:
                output = read_hook_output(process, command)
        reply = json.loads(output.decode("utf-8"))
        if not isinstance(reply, dict):
            raise ValueError("hot-upgrade hook must return a JSON object")
        return reply

    return invoke


def read_hook_output(process, command):
    deadline = time.monotonic() + HOOK_TIMEOUT
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.select(max(0, deadline - time.monotonic())):
                chunk = os.read(process.stdout.fileno(), HOOK_RESPONSE_LIMIT + 1 - len(output))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > HOOK_RESPONSE_LIMIT:
                    raise ValueError("hot-upgrade hook response exceeds 64 KiB")
            else:
                raise subprocess.TimeoutExpired(command, HOOK_TIMEOUT)
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise
    if process.returncode:
        raise RuntimeError("hot-upgrade hook exited unsuccessfully")
    return output


class Supervisor:
    permit_manager = "supervisord"

    def __init__(self, base, *, recovery=False):
        self.base = base
        self.recovery = recovery
        self.api = None
        if not recovery or ctl.daemon_pid():
            self.connect()

    def connect(self):
        self.api = ctl.connection()
        ctl.active_programs(self.api)
        self.plan = json.loads(ctl.SNAPSHOT.read_text())
        if self.plan["release"] != str(self.base) or not self.plan["default"]:
            raise ValueError("reconcile the installed supervisor baseline before a per-service handoff")

    def pid(self, name):
        if self.api is None:
            return 0
        if name not in self.plan["services"]:
            raise ValueError(f"service absent from the installed supervisor plan: {name}")
        try:
            row = self.api.getProcessInfo(name)
        except Fault as error:
            if error.faultCode == 10:  # BAD_NAME, after interrupted group replacement.
                return 0
            raise
        return row["pid"] if row["statename"] == "RUNNING" else 0

    def stop(self, name):
        ctl.stop_group(self.api, name)
        row = self.api.getProcessInfo(name)
        if row["pid"] or row["statename"] != "STOPPED":
            raise RuntimeError("incumbent supervisor group did not stop")

    def start(self, name, service):
        if self.api is None:
            self.bootstrap_for_recovery()
        if self.recovery:
            self.prepare_recovery_dependencies(name)
        self.plan["services"][name] = service
        ctl.render(self.plan)
        added, changed, removed = self.api.reloadConfig()[0]
        if name in removed:
            raise RuntimeError("target supervisor group disappeared from the plan")
        if name in changed:
            self.api.removeProcessGroup(name)
        if name in changed or name in added:
            self.api.addProcessGroup(name)
        self.api.startProcess(name, True)

    def bootstrap_for_recovery(self):
        plan = ctl.build_plan(release=self.base, use_pins=True)
        ctl.prepare_data_root(plan)
        ctl.render(plan)
        # Rendered groups have autostart=false. Bring back the manager before
        # touching the selected service; other jobs remain stopped.
        ctl.start_daemon()
        self.connect()

    def prepare_recovery_dependencies(self, name):
        plan = self.plan
        if name in {"web", "web-notifier"} and plan["web_database"] != "external":
            postgres = plan["services"]["postgres"]
            if postgres["skip"]:
                raise RuntimeError("managed PostgreSQL is unavailable for recovery")
            if not self.pid("postgres"):
                self.api.startProcess("postgres", True)
            ctl.await_postgres(postgres)
        if name == "web" and plan["services"]["web"]["env"].get("WEB_REQUIRE_FINANCE") == "true":
            ctl.check_queue_compatibility(plan)
            if not self.pid("finance-api"):
                self.api.startProcess("finance-api", True)


class Systemd:
    permit_manager = "systemd"

    def __init__(self, base):
        self.base = base

    def unit(self, name):
        unit = units.service_unit(name)
        marker = units.matching_unit(units.UNIT_DIR / unit, ctl.ROOT)
        state = units.unit_state(unit)[unit]
        if (not marker or marker["release"] != self.base.name
                or state["FragmentPath"] != str(units.UNIT_DIR / unit)):
            raise ValueError(f"service outside the installed systemd baseline: {name}")
        return unit, state

    def pid(self, name):
        _, state = self.unit(name)
        return int(state["MainPID"]) if state["ActiveState"] == "active" else 0

    def stop(self, name):
        unit, _ = self.unit(name)
        units.systemctl("stop", unit, check=True, timeout=125)
        _, state = self.unit(name)
        if state["ActiveState"] != "inactive" or int(state["MainPID"]):
            raise RuntimeError("incumbent systemd unit did not stop")

    def start(self, name, _service):
        unit, _ = self.unit(name)
        units.systemctl("start", unit, check=True, timeout=125)


def start_handoff(manager, name, service, record):
    host = getattr(type(manager), "permit_manager", None)
    if host is None:
        return manager.start(name, service)
    release = Path(service["release"]).name
    with handoff_start.authorize(ctl.ROOT, record, release, host) as permit:
        manager.start(name, service)
        handoff_start.wait_for_launch(permit)


def local_probe(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("hot-upgrade probes require an explicit loopback HTTP port")
    return parsed.port


def owns_listener(pid, port):
    """A healthy response must come from this release's own loopback listener."""
    process = Path("/proc") / str(pid)
    try:
        sockets = {os.readlink(fd) for fd in (process / "fd").iterdir() if fd.is_symlink()}
        address = f"0100007F:{port:04X}"
        for line in (process / "net/tcp").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == address and fields[3] == "0A" and f"socket:[{fields[9]}]" in sockets:
                return True
    except (FileNotFoundError, ProcessLookupError):
        return False  # Process exited or closed a socket during inspection.
    return False


def wait_ready(pid, service, *, timeout=35):
    probes = [service[key] for key in ("live_url", "ready_url") if service[key]]
    endpoints = [(url, local_probe(url)) for url in probes]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = pid()
        if observed and all(owns_listener(observed, port) and ctl.http_status(url, "fertile-hot-upgrade") == 200
                            for url, port in endpoints) and pid() == observed:
            return observed
        time.sleep(.1)
    raise RuntimeError("service did not become ready")


def ready_evidence(hook, step, record, pid, service):
    observed = wait_ready(pid, service)
    runtime = {key: service[key] for key in ("live_url", "ready_url")}
    response = hook(step, {**record, "runtime": {**runtime, "pid": observed}})
    if pid() != observed:
        raise RuntimeError("the process changed while verifying application readiness")
    return response


def candidate_exec(expected_parent):
    hot_upgrade.require_parent(expected_parent)
    service = json.load(sys.stdin)
    env = ctl.local_environment(service)
    port_env, bind_env = READER_ENV[service["handoff_name"]]
    env[port_env] = service["env"][port_env]
    env[bind_env] = "127.0.0.1"
    if service["handoff_name"] == "web":
        disabled_token = Path(service["release"]) / ".candidate-disabled-token"
        if os.path.lexists(disabled_token):
            raise ValueError("candidate token placeholder must be absent")
        env["WEB_ADMIN_TOKEN_FILE"] = str(disabled_token)
    os.chdir(service["directory"])
    os.execvpe(service["command"][0], service["command"], env)


class Handoff:
    def __init__(self, name, base, target, service, manager, hook):
        self.name = name
        self.base = base
        self.target = target
        self.service = service
        self.manager = manager
        self.hook = hook
        self.candidate = None
        self.candidate_plan = None

    def start_candidate(self):
        port_env, _ = READER_ENV[self.name]
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = str(reserved.getsockname()[1])
        self.candidate_plan = ctl.build_plan(release=self.target, names={self.name},
                                             settings_overrides={port_env: port})["services"][self.name]
        self.candidate_plan["handoff_name"] = self.name
        self.candidate_plan["pinned"] = True
        if self.name == "web":
            self.candidate_plan["env"]["WEB_AUTO_MIGRATE"] = "false"
        self.candidate = subprocess.Popen([sys.executable, __file__, "_candidate", str(os.getpid())],
                                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True)
        with self.candidate.stdin as stream:
            stream.write(json.dumps(self.candidate_plan).encode())

    def close_candidate(self):
        proc, self.candidate = self.candidate, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def start_target(self, record):
        if self.name == "finance-api":
            # The incumbent may have accepted another job after preflight.
            ctl.check_queue_compatibility({
                "default": True, "release": str(self.target), "data": self.service["env"]["FERTILE_DATA_ROOT"],
            })
        service_releases.select(ctl.ROOT, self.base, self.name, self.target)
        start_handoff(self.manager, self.name, self.service, record)

    def __call__(self, step, record):
        if self.name in READER_ENV:
            if step == "candidate-start":
                self.start_candidate()
                return {}
            if step == "candidate-ready":
                proc = self.candidate
                return ready_evidence(self.hook, step, record,
                                      lambda: proc.pid if proc.poll() is None else 0, self.candidate_plan)
            if step == "candidate-abort":
                self.close_candidate()
                return {}
        if step == "reader-cutover":
            if self.name not in READER_ENV:
                # Close the separate, non-publishing projector before the
                # named manager starts the selected production projector.
                if self.hook(step, record) != {}:
                    raise ValueError("projector cutover hook must acknowledge with an empty JSON object")
            self.close_candidate()
            self.manager.stop(self.name)
            self.start_target(record)
            return {}
        if step in {"collector-release", "worker-stop"}:
            self.manager.stop(self.name)
            if step == "worker-stop":
                return {}
        if step in {"collector-start", "worker-start"}:
            self.start_target(record)
            return {}
        if step == "worker-drain" and self.name == "web-notifier":
            notifier_plan = ctl.build_plan(release=self.target, names={"postgres", "web", self.name})
            ctl.prepare_notifier(notifier_plan)
        if step in {"reader-ready", "collector-ready", "worker-ready"}:
            return ready_evidence(self.hook, step, record, lambda: self.manager.pid(self.name), self.service)
        return self.hook(step, record)


def installed(*, recovery=False):
    if os.environ.get("FERTILE_SERVICE_MANIFEST"):
        raise ValueError("hot-upgrade uses the installed release manifest")
    base = units.managed_release(ctl.ROOT)
    if base:
        # Match systemd-service's runtime file precedence.
        for key in ctl.SUPPORTED:
            os.environ.pop(key, None)
        manager = Systemd(base)
    else:
        base = ctl.release_path()
        if not recovery and not ctl.daemon_pid():
            raise RuntimeError("start the installed supervisor before a per-service handoff")
        manager = Supervisor(base, recovery=recovery)
    if not (base / "fertile/scripts/handoff_start.py").is_file():
        raise ValueError("install a baseline release with hot-upgrade start fencing first")
    return base, manager


def effective_service(release, name):
    plan = ctl.build_plan(release=release, names={name})
    service = plan["services"].get(name)
    if service is None or service["skip"]:
        raise ValueError(f"{name} is unavailable in {release.name}")
    return plan, service


def volume_layout(service):
    release = Path(service["release"])
    return {str(Path("$release") / Path(path).relative_to(release)) if Path(path).is_relative_to(release)
            else path: mode for path, mode in service["volumes"].items()}


def upgrade(args, hook):
    with hot_upgrade.lock(ctl.ROOT):
        hot_upgrade.require_clear(ctl.ROOT)
        base, manager = installed()
        name = args.service
        previous = service_releases.selected(ctl.ROOT, base, name)
        target = service_releases.release_path(ctl.ROOT, args.release)
        if previous == target:
            raise ValueError("service already selects this release")
        verify_release(target)
        _, current = effective_service(previous, name)
        next_plan, candidate = effective_service(target, name)
        if (any(current.get(key) != candidate.get(key) for key in TOPOLOGY)
                or volume_layout(current) != volume_layout(candidate)):
            raise ValueError("service topology or health endpoints changed; install in a maintenance window")
        if not manager.pid(name):
            raise RuntimeError("incumbent service is not running")
        if isinstance(manager, Supervisor):
            running = manager.plan["services"][name]
            if running.get("release", manager.plan["release"]) != str(previous):
                raise ValueError("active supervisor plan and selected release disagree")
            if any(running["env"].get(key) != current["env"].get(key) for key in INSTANCE_ENV):
                raise ValueError("instance settings changed since the incumbent started")
        role = current["role"]
        if role in {"notifier", "decision"}:
            role = "worker"
        if role not in hot_upgrade.ROLES or (role == "reader" and name not in READER_ENV):
            raise ValueError(f"service has no hot-upgrade handoff: {name}")
        if role == "reader":
            if not current["ready_url"] or not candidate["ready_url"]:
                raise ValueError("reader handoff needs a canonical readiness probe")
            if not args.rollback:
                wait_ready(lambda: manager.pid(name), current, timeout=5)
        if args.migrate and args.rollback:
            raise ValueError("rollback cannot apply a migration")
        if name == "finance-api":
            ctl.check_queue_compatibility(next_plan)
        candidate["pinned"] = True
        if name == "web":
            candidate["env"]["WEB_AUTO_MIGRATE"] = "false"
        handoff = Handoff(name, base, target, candidate, manager, hook)
        try:
            return hot_upgrade.perform(ctl.ROOT, handoff, service=name, role=role,
                                       previous=previous.name, target=target.name,
                                       source=current["source"] if role == "collector" else None,
                                       direction="rollback" if args.rollback else "upgrade",
                                       migrate=args.migrate, lock_held=True)
        finally:
            handoff.close_candidate()


def reconcile(args, hook):
    with hot_upgrade.lock(ctl.ROOT):
        base, manager = installed(recovery=True)

        def verify(step, record):
            result = hook(step, record)
            service = record["service"]
            active = service_releases.selected(ctl.ROOT, base, service)
            if (not isinstance(result, dict) or result.get("reconciled") is not True
                    or active.name != result.get("active_release")
                    or active.name not in {record["previous"], record["target"]}):
                raise ValueError("selected release and manager disagree with reconciliation")
            plan = ctl.build_plan(release=base, names={service}, use_pins=True)
            row = plan["services"][service]
            if row["skip"]:
                raise ValueError("selected service is unavailable")
            if not manager.pid(service):
                if result.get("safe_to_start") is not True:
                    raise ValueError("hook must explicitly authorize recovery of the stopped service")
                if service == "web" and not row["pinned"]:
                    # A previous attempt may already have applied expansion SQL.
                    service_releases.select(ctl.ROOT, base, service, active)
                    row = ctl.build_plan(release=base, names={service}, use_pins=True)["services"][service]
                if service == "finance-api":
                    ctl.check_queue_compatibility(plan)
                start_handoff(manager, service, row, record)
            if isinstance(manager, Supervisor):
                running = manager.plan["services"][service]
                if running.get("release", manager.plan["release"]) != str(active):
                    raise ValueError("rendered supervisor plan disagrees with reconciliation")
            readiness = ready_evidence(hook, "reconcile-ready", record, lambda: manager.pid(service), row)
            if not isinstance(readiness, dict) or readiness.get("ready") is not True:
                raise ValueError("hook must confirm application readiness after recovery")
            return result

        return hot_upgrade.reconcile(ctl.ROOT, args.id, verify, lock_held=True)


def main():
    if sys.argv[1:2] == ["_candidate"]:
        candidate_exec(int(sys.argv[2]))
        return
    parser = argparse.ArgumentParser(prog="fertile")
    actions = parser.add_subparsers(dest="action", required=True)
    change = actions.add_parser("hot-upgrade")
    change.add_argument("service")
    change.add_argument("release")
    change.add_argument("--hook", required=True)
    change.add_argument("--migrate", action="store_true")
    change.add_argument("--rollback", action="store_true")
    status = actions.add_parser("hot-upgrade-status")
    status.add_argument("--json", action="store_true")
    recovery = actions.add_parser("hot-upgrade-reconcile")
    recovery.add_argument("id")
    recovery.add_argument("--hook", required=True)
    args = parser.parse_args()
    if args.action == "hot-upgrade-status":
        records = hot_upgrade.records(ctl.ROOT)
        if args.json:
            print(json.dumps(records))
        else:
            for item in records:
                print(item["id"], item["service"], item["status"], item["step"])
        return
    if os.geteuid() == 0:
        raise PermissionError("run hot-upgrade as the non-root service account")
    hook = hook_command(args.hook)
    result = upgrade(args, hook) if args.action == "hot-upgrade" else reconcile(args, hook)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, Fault, subprocess.SubprocessError) as error:
        sys.exit(f"fertile: {error}")
