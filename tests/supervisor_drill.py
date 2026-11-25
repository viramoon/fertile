"""Linux fault drills for one configured fertile supervisor. Requires pidfd support."""
from contextlib import ExitStack
import datetime
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from urllib.request import urlopen


CLI = Path(__file__).resolve().parents[1] / "scripts/fertile"
ROOT = Path(os.environ.get("FERTILE_ROOT", Path(os.environ.get("FERTILE_BOX_HOME", "/home/box")) / "fertile")).resolve()
SKIPPED = {"disabled", "missing_binary", "needs_credentials"}


def report(event, **details):
    print(json.dumps({"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      "event": event, **details}, ensure_ascii=False), flush=True)


def status(timeout=8):
    out = subprocess.check_output([str(CLI), "status", "--json"], timeout=timeout)
    return json.loads(out)


def identity(pid, *, parent=None, daemon=False):
    """Get a stable process reference before checking its identity; never signal a reused PID."""
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        raise RuntimeError("invalid process ID")
    fd = os.pidfd_open(pid)
    try:
        if parent is not None:
            owner = next(s for s in Path(f"/proc/{pid}/status").read_text().splitlines() if s.startswith("PPid:"))
            if int(owner.split()[1]) != parent:
                raise RuntimeError(f"PID {pid} is outside expected parent {parent}")
        if daemon:
            config = str(ROOT / "run/supervisord.conf")
            argv = [os.fsdecode(i) for i in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")]
            if not any(Path(a).name == "supervisord" for a in argv) or config not in argv:
                raise RuntimeError(f"PID {pid} is not supervisord using {config}")
            if int((ROOT / "run/supervisord.pid").read_text()) != pid:
                raise RuntimeError("supervisor status and pid file disagree")
        return fd
    except BaseException:
        os.close(fd)
        raise


def running_initial():
    state = status()
    if state["supervisor"] != "RUNNING":
        raise RuntimeError("start the selected fertile stack before running this drill")
    active = {}
    skipped = {}
    for name, svc in state["services"].items():
        if svc["state"] in SKIPPED:
            skipped[name] = svc["state"]
        elif svc["state"] != "RUNNING" or svc["pid"] <= 1:
            raise RuntimeError(f"baseline service {name} is not running: {svc['state']}")
        else:
            active[name] = svc
    if not active:
        raise RuntimeError("no running supervised programs")
    return state["supervisor_pid"], active, skipped


def seconds_left(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("supervisor drill recovery deadline exceeded")
    return remaining


def wait_status(deadline, accepts):
    while True:
        state = status(timeout=seconds_left(deadline))
        if accepts(state):
            seconds_left(deadline)
            return state
        time.sleep(min(.15, seconds_left(deadline)))


def chaos():
    daemon, active, _ = running_initial()
    api = active.get("finance-api")
    if api is None or not api.get("live_url") or api["live"] != 200:
        raise RuntimeError("finance-api with a healthy live endpoint is required")
    with ExitStack() as stack:
        supervisor = identity(daemon, daemon=True)
        stack.callback(os.close, supervisor)
        target = identity(api["pid"], parent=daemon)
        stack.callback(os.close, target)
        report("kill -9 finance-api", supervisor_pid=daemon, old_pid=api["pid"], live_url=api["live_url"])
        started = time.monotonic()
        deadline = started + 10
        signal.pidfd_send_signal(target, signal.SIGKILL)
        def restarted(state):
            svc = state["services"]["finance-api"]
            return (state["supervisor_pid"] == daemon and svc["state"] == "RUNNING"
                    and svc["pid"] not in (0, api["pid"]) and svc["live"] == 200)
        new = wait_status(deadline, restarted)["services"]["finance-api"]
        with urlopen(api["live_url"], timeout=seconds_left(deadline)) as response:
            if response.status != 200:
                raise RuntimeError(f"independent live GET returned {response.status}")
        seconds_left(deadline)
        report("finance-api automatically recovered", new_pid=new["pid"], http=response.status,
               recovery_seconds=round(time.monotonic() - started, 3))


def children(pid):
    # Some box/container procfs implementations expose an empty task/children
    # file even for a live parent. STAT's documented PPID also works there.
    found = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if int(fields[1]) == pid and fields[0] != "Z":
            found.add(int(process.name))
    return found


def freeze(pid, fd):
    try:
        signal.pidfd_send_signal(fd, signal.SIGSTOP)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if select.select([fd], [], [], 0)[0] or stat == "Z":
                return False
            if stat in {"T", "t"}:
                return True
            time.sleep(.01)
    except (FileNotFoundError, ProcessLookupError):
        if select.select([fd], [], [], 0)[0]:
            return False
        raise
    raise TimeoutError(f"could not freeze PID {pid}")


def crash_tree(daemon, expected):
    # Freeze the checked daemon first, so it cannot restart services while their descendants
    # are being enumerated. Each signal uses an open pidfd and a verified parent relationship.
    with ExitStack() as stack:
        processes = []
        root_fd = identity(daemon, daemon=True)
        stack.callback(os.close, root_fd)
        processes.append((daemon, root_fd))
        try:
            if not freeze(daemon, root_fd):
                raise RuntimeError("supervisor exited before the drill")
            direct = children(daemon)
            if direct != set(expected):
                raise RuntimeError(f"supervisor children changed: expected {sorted(expected)}, found {sorted(direct)}")
            index = 0
            while index < len(processes):
                parent, _ = processes[index]
                index += 1
                for child in sorted(children(parent)):
                    try:
                        fd = identity(child, parent=parent)
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    stack.callback(os.close, fd)
                    processes.append((child, fd))
                    if not freeze(child, fd):
                        processes.pop()  # It exited ahead of us; never enumerate a recycled PID.
            report("kill -9 supervised tree", supervisor_pid=daemon, pids=[pid for pid, _ in processes])
            started = time.monotonic()
            for _, fd in reversed(processes):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            for pid, fd in processes:
                if not select.select([fd], [], [], seconds_left(started + 8))[0]:
                    raise TimeoutError(f"old PID {pid} has not exited")
            return started
        finally:
            # A failed preflight must not leave a live process suspended.
            for _, fd in reversed(processes):
                try:
                    signal.pidfd_send_signal(fd, signal.SIGCONT)
                except ProcessLookupError:
                    pass


def reboot():
    daemon, active, skipped = running_initial()
    for name, svc in active.items():
        if any(svc.get(k + "_url") and svc[k] != 200 for k in ("live", "ready")):
            raise RuntimeError(f"baseline health failed for {name}")
    started = crash_tree(daemon, [svc["pid"] for svc in active.values()])
    deadline = started + 60
    subprocess.run([str(CLI), "ensure"], check=True, timeout=seconds_left(deadline))
    def recovered(state):
        if state["supervisor"] != "RUNNING" or state["supervisor_pid"] == daemon:
            return False
        for name, before in active.items():
            after = state["services"][name]
            if after["state"] != "RUNNING" or after["pid"] in (0, before["pid"]):
                return False
            if any(before.get(k + "_url") and after[k] != 200 for k in ("live", "ready")):
                return False
        return all(state["services"][name]["state"] == reason for name, reason in skipped.items())
    state = wait_status(deadline, recovered)
    report("fertile ensure recovered enabled services", supervisor_pid=state["supervisor_pid"],
           processes={name: {k: state["services"][name][k] for k in ("pid", "live", "ready")}
                      for name in active}, skipped=skipped,
           recovery_seconds=round(time.monotonic() - started, 3))


if __name__ == "__main__":
    {"chaos": chaos, "reboot": reboot}[sys.argv[1]]()
