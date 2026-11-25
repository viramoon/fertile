#!/usr/bin/env python3
"""Run scoped process and database recovery drills against the selected deployment."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import supervisor_drill as drill


def healthy(service):
    return service["state"] == "RUNNING" and all(
        not service.get(key + "_url") or service[key] == 200 for key in ("live", "ready")
    )


def smoke():
    subprocess.run([str(drill.CLI.parents[1] / "tests/e2e-smoke.sh"), "local"], check=True, timeout=190)


def check_processes():
    daemon, services, skipped = drill.running_initial()
    drill.report("baseline", enabled=list(services), skipped=skipped)
    if not all(healthy(svc) for svc in services.values()):
        raise RuntimeError("all participating services must be healthy before drill")
    for name in services:
        before = drill.status()
        svc = before["services"][name]
        if before["supervisor_pid"] != daemon or not healthy(svc):
            raise RuntimeError(f"unstable baseline for {name}")
        with ExitStack() as stack:
            stack.callback(os.close, drill.identity(daemon, daemon=True))
            process = drill.identity(svc["pid"], parent=daemon)
            stack.callback(os.close, process)
            started = time.monotonic()
            signal.pidfd_send_signal(process, signal.SIGKILL)
            state = drill.wait_status(started + 30, lambda s:
                s["supervisor_pid"] == daemon and healthy(s["services"][name])
                and s["services"][name]["pid"] not in (0, svc["pid"]))
            if name == "ib-gateway":
                subprocess.run([str(drill.CLI.with_name("ib-check.sh"))], check=True,
                               timeout=drill.seconds_left(started + 30))
            seconds = time.monotonic() - started
            if seconds > 30:
                raise TimeoutError(f"{name}: exceeded 30s")
            drill.report("SIGKILL recovered", service=name, old_pid=svc["pid"],
                         new_pid=state["services"][name]["pid"], recovery_seconds=round(seconds, 3))
        smoke()


def restart_database():
    daemon, services, _ = drill.running_initial()
    required = ("web", "finance-api", "postgres")
    if not all(k in services and healthy(services[k]) for k in required):
        raise RuntimeError("need a healthy postgres, finance-api and web")
    with ExitStack() as stack:
        stack.callback(os.close, drill.identity(daemon, daemon=True))
        stack.callback(os.close, drill.identity(services["postgres"]["pid"], parent=daemon))
        supervisor_bin = Path(os.environ.get("FERTILE_SUPERVISOR_BIN", drill.ROOT / "tools/supervisor/bin"))
        control = [str(supervisor_bin / "supervisorctl"), "-c", str(drill.ROOT / "run/supervisord.conf")]
        stopped = time.monotonic()
        subprocess.run([drill.CLI, "stop", "postgres"], check=True, timeout=35)
        drill.report("postgres clean stop", elapsed_seconds=round(time.monotonic() - stopped, 3))
        try:
            outage = drill.wait_status(time.monotonic() + 30, lambda s: s["services"]["web"]["ready"] == 503)
            drill.report("postgres offline", web_ready=outage["services"]["web"]["ready"],
                         finance_ready=outage["services"]["finance-api"]["ready"])
        finally:
            started = time.monotonic()
            subprocess.run([*control, "start", "postgres"], check=True, timeout=30)
        resumed = drill.wait_status(started + 30, lambda s: all(healthy(s["services"][n]) for n in required))
        for name in ("web", "finance-api"):
            if resumed["services"][name]["pid"] != services[name]["pid"]:
                raise RuntimeError(f"{name} restarted during database interruption")
        drill.report("database recovered", web_pid=services["web"]["pid"], finance_pid=services["finance-api"]["pid"],
                     recovery_seconds=round(time.monotonic() - started, 3))
    smoke()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("services", "postgres", "reboot"))
    scenario = parser.parse_args().scenario
    if scenario == "services":
        check_processes()
    elif scenario == "postgres":
        restart_database()
    else:
        drill.reboot()
        smoke()
