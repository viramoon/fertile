"""Run the active manifest's core services inside their systemd units."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import supervision as ctl
from systemd_units import SERVICES, TARGET, UNIT_DIR, service_unit, unit_state


STATES = {"active": "RUNNING", "activating": "STARTING", "deactivating": "STOPPING",
          "reloading": "RUNNING", "inactive": "STOPPED", "failed": "FATAL"}


def plan_for_release():
    release = ctl.SCRIPTS.parents[1]
    if release.parent != ctl.ROOT / "releases":
        raise ValueError("systemd launcher must be in a built release under FERTILE_ROOT/releases")
    plan = ctl.build_plan(release=release, names=SERVICES)
    if tuple(plan["services"]) != SERVICES:
        raise ValueError("the release manifest disagrees with the systemd core service order")
    return plan


def start(plan, action, name):
    if (ctl.RUN / "restore-journal.json").exists():
        raise RuntimeError("unfinished restore; run scripts/restore.sh to recover before starting services")
    if ctl.daemon_pid():
        raise RuntimeError("stop the existing supervisord stack before starting systemd units")
    service = plan["services"][name]
    if service["skip"]:
        raise ValueError(f"{name}: {service['skip']}")
    if action == "prepare":
        ctl.check_queue_compatibility(plan)
        ctl.prepare_data_root(plan)
        if name == "postgres":
            subprocess.run([service["command"][0], "init"], env=service["env"], check=True)
        if name == "web":
            ctl.prepare_web(plan)
    elif action == "ready":
        if name != "postgres":
            raise ValueError(f"no systemd startup readiness command for {name}")
        env = {**service["env"], "PGDATABASE": "postgres"}
        for _ in range(100):
            if subprocess.run(["pg_isready", "-q"], env=env).returncode == 0:
                return
            time.sleep(.1)
        raise RuntimeError("PostgreSQL did not become ready")
    else:
        env = dict(service["env"])
        if service["env_file"]:
            env.update(ctl.read_env(ctl.ROOT / "secrets" / service["env_file"], secret=True))
        os.chdir(service["directory"])
        os.execvpe(service["command"][0], service["command"], env)


def report(plan):
    states = unit_state(TARGET, *(service_unit(name) for name in SERVICES))
    services = {}
    user_agent = "fertile-supervision/" + (ctl.SCRIPTS.parent / "VERSION").read_text().strip()
    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = {}
        for name, service in plan["services"].items():
            unit = service_unit(name)
            state = states[unit]
            if state["FragmentPath"] != str(UNIT_DIR / unit):
                raise RuntimeError(f"unexpected systemd unit file for {unit}")
            item = services[name] = {
                "enabled": service["enabled"], "state": STATES.get(state["ActiveState"], state["ActiveState"]),
                "pid": int(state["MainPID"]), "live": None, "ready": None,
                "live_url": service["live_url"], "ready_url": service["ready_url"],
            }
            if item["state"] == "RUNNING":
                for check in ("live", "ready"):
                    pending[name, check] = pool.submit(ctl.http_status, item[check + "_url"], user_agent)
        for (name, check), result in pending.items():
            services[name][check] = result.result()
    target = states[TARGET]["ActiveState"]
    return {"manager": "systemd", "systemd": target, "supervisor": STATES.get(target, target),
            "supervisor_pid": 0, "services": services}


def main():
    parser = argparse.ArgumentParser(prog="fertile")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status").add_argument("--json", action="store_true")
    for action in ("prepare", "run", "ready"):
        commands.add_parser(action).add_argument("name", choices=SERVICES)
    args = parser.parse_args()
    # Unit configuration comes from runtime.env. An interactive status call
    # must not probe different ports because its caller exported overrides.
    for key in ctl.SUPPORTED:
        os.environ.pop(key, None)
    plan = plan_for_release()
    if args.action == "status":
        data = report(plan)
        if args.json:
            print(json.dumps(data))
        else:
            print(f"systemd {TARGET} {data['systemd']}")
            for name, item in data["services"].items():
                print(f"{name:24} {item['state']:24} pid={item['pid']} live={item['live']} ready={item['ready']}")
    else:
        start(plan, args.action, args.name)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f"fertile: {error}")
