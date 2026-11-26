"""Run the active manifest's core services inside their systemd units."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from handoff_start import require_start
import supervision as ctl
from systemd_units import SYSTEMD_ROWS, TARGET, UNIT_DIR, host_rows, service_unit, unit_state, validate_configured_roots


STATES = {"active": "RUNNING", "activating": "STARTING", "deactivating": "STOPPING",
          "reloading": "RUNNING", "inactive": "STOPPED", "failed": "FATAL"}


def plan_for_release():
    release = ctl.SCRIPTS.parents[1]
    if release.parent != ctl.ROOT / "releases":
        raise ValueError("systemd launcher must be in a built release under FERTILE_ROOT/releases")
    names = tuple(row["name"] for row in host_rows(release))
    plan = ctl.build_plan(release=release, names=names, use_pins=True)
    if tuple(plan["services"]) != names:
        raise ValueError("the release manifest disagrees with the systemd core service order")
    return plan


def start(plan, action, name):
    if (ctl.RUN / "restore-journal.json").exists():
        raise RuntimeError("unfinished restore; run scripts/restore.sh to recover before starting services")
    if ctl.daemon_pid():
        raise RuntimeError("stop the existing supervisord stack before starting systemd units")
    service = plan["services"][name]
    release = Path(service["release"]).name
    require_start(ctl.ROOT, name, release, "systemd")
    if action == "condition":
        if service["skip"]:
            return 1
        validate_configured_roots(plan, name)
        return 0
    if service["skip"]:
        raise ValueError(f"{name}: {service['skip']}")
    validate_configured_roots(plan, name)
    if action == "prepare":
        ctl.check_queue_compatibility(plan)
        ctl.prepare_data_root(plan)
        if name == "postgres":
            subprocess.run([service["command"][0], "init"], env=service["env"], check=True)
        if name == "web":
            ctl.prepare_web(plan)
        elif service["role"] == "notifier":
            ctl.prepare_notifier(plan)
    elif action == "ready":
        if service["startup"] != "postgres":
            raise ValueError(f"no systemd startup readiness command for {name}")
        ctl.await_postgres(service)
    else:
        env = ctl.local_environment(service)
        os.chdir(service["directory"])
        require_start(ctl.ROOT, name, release, "systemd", launch=True)
        os.execvpe(service["command"][0], service["command"], env)


def report(plan):
    states = unit_state(TARGET, *(service_unit(name) for name in plan["services"]))
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
                "enabled": service["enabled"],
                "state": service["skip"] or STATES.get(state["ActiveState"], state["ActiveState"]),
                "pid": int(state["MainPID"]), "live": None, "ready": None,
                "live_url": service["live_url"], "ready_url": service["ready_url"],
                "release": service["release"],
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
    for action in ("prepare", "run", "ready", "condition"):
        commands.add_parser(action).add_argument("name", choices=SYSTEMD_ROWS)
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
        return start(plan, args.action, args.name)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f"fertile: {error}")
