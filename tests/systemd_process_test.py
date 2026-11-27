"""Run generated Fertile units under a disposable systemd PID 1.

The fixture records only process identity, descriptor limits and TERM handling.
The image is prepared separately, then run without networking or host volumes.
"""

import argparse
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time


REPO = Path(__file__).resolve().parents[1]
DOCKER = ("docker", "--host=unix:///var/run/docker.sock")
KIT = "/tmp/fertile-systemd-kit"
ACCOUNT = "fertile-drill"
COMMIT = "a" * 40
CONTROL = Path("/opt/fertile-control")
SERVICES = ("finance-api", "finance-collector")
SYSTEMCTL = ("/usr/bin/systemctl", "--system", "--no-pager", "--no-ask-password")


PROCESS = """\
#!/usr/bin/python3
import json
import os
from pathlib import Path
import resource
import signal
import time

name = Path(__file__).name
data = Path(os.environ["FERTILE_DATA_ROOT"])

def stopped(*_):
    (data / (name + ".stopped")).write_text(str(os.getpid()))
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stopped)
temp = data / (name + ".tmp")
temp.write_text(json.dumps({"pid": os.getpid(), "uid": os.getuid(),
                            "limit": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
                            "source": os.environ["FERTILE_MARKET_SOURCE"],
                            "data": str(data)}))
temp.replace(data / (name + ".json"))
while True:
    time.sleep(.1)
"""


def command(*argv, timeout=25, check=True, **kwargs):
    return subprocess.run(argv, timeout=timeout, check=check, **kwargs)


def wait_for(observe, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = observe()
        if result:
            return result
        time.sleep(.1)
    raise RuntimeError(f"timed out waiting for {description}")


def prepare(root, account, control=CONTROL):
    release = root / "releases" / COMMIT
    fertile = release / "fertile"
    fertile.mkdir(parents=True)
    for directory in ("scripts", "deploy"):
        shutil.copytree(REPO / directory, fertile / directory,
                        ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "VERSION", fertile / "VERSION")
    (root / "current").symlink_to(release.relative_to(root), target_is_directory=True)
    (root / "tools").mkdir()
    (root / "tools/supervisor").symlink_to(control, target_is_directory=True)
    (release / "runtime").mkdir()
    (release / "runtime/venv").symlink_to(control, target_is_directory=True)
    (root / "config").mkdir()
    (root / "config/runtime.env").write_text("FERTILE_PROFILE=stable\nFERTILE_MARKET_SOURCE=replay\n")
    # The launcher selects its existing control environment from the fixture root.
    (root / "env.sh").write_text("export PATH=/usr/bin:/bin\n")
    config = release / "finance/config/collector"
    config.mkdir(parents=True)
    (config / "stable.yaml").write_text("source: replay\n")
    data = root / "data/replay"
    data.mkdir(parents=True)
    os.chown(data, account.pw_uid, account.pw_gid)
    binaries = release / "bin"
    binaries.mkdir()
    for service in SERVICES:
        app = binaries / service
        app.write_text(PROCESS)
        app.chmod(0o755)
    return data


def unit(service):
    return f"fertile-{service}.service"


def systemd_field(service, field):
    return command(*SYSTEMCTL, "show", "--value", f"--property={field}", unit(service),
                   stdout=subprocess.PIPE, text=True).stdout.strip()


def process_state(data, service, previous=0):
    state = data / f"{service}.json"
    if not state.exists():
        return None
    observed = json.loads(state.read_text())
    pid = int(systemd_field(service, "MainPID"))
    if not pid or pid == previous or observed["pid"] != pid:
        return None
    return observed


def drill(root, data, account):
    command(REPO / "scripts/fertile", "install-systemd", "--root", root,
            "--user", ACCOUNT, "--group", ACCOUNT)
    try:
        command(*SYSTEMCTL, "start", "fertile.target")
        processes = {}
        for service in SERVICES:
            observed = wait_for(lambda: process_state(data, service), service)
            if (observed["limit"], observed["source"], observed["data"], observed["uid"]) != (
                    4096, "replay", str(data), account.pw_uid):
                raise RuntimeError(f"incorrect generated launcher settings for {service}")
            groups = Path(f"/proc/{observed['pid']}/cgroup").read_text().splitlines()
            if not any(group.endswith(f"/{unit(service)}") for group in groups):
                raise RuntimeError(f"{service} did not run inside its generated unit")
            if systemd_field(service, "FragmentPath") != f"/etc/systemd/system/{unit(service)}":
                raise RuntimeError(f"systemd used the wrong unit for {service}")
            processes[service] = observed["pid"]

        command(*SYSTEMCTL, "kill", "--kill-whom=main", "--signal=SIGKILL", unit("finance-api"))
        respawn = wait_for(lambda: process_state(data, "finance-api", processes["finance-api"]), "API restart")
        processes["finance-api"] = respawn["pid"]
        if int(systemd_field("finance-collector", "MainPID")) != processes["finance-collector"]:
            raise RuntimeError("collector restarted with the API")
    finally:
        command(*SYSTEMCTL, "stop", "fertile.target")

    for service, pid in processes.items():
        if (systemd_field(service, "ActiveState") != "inactive"
                or (data / f"{service}.stopped").read_text() != str(pid)):
            raise RuntimeError(f"{service} did not stop through the generated target")
    print("PASS generated systemd target starts, restarts and stops synthetic services")


def inside():
    caller = os.environ.get("FERTILE_DRILL_CALLER_PID_NAMESPACE")
    if (os.geteuid() != 0 or not Path("/.dockerenv").is_file()
            or Path("/proc/1/comm").read_text().strip() != "systemd"
            or REPO != Path(KIT) or not caller
            or caller == os.readlink("/proc/self/ns/pid")):
        raise RuntimeError("the drill requires its dedicated Docker boot and fixture path")
    account = pwd.getpwnam(ACCOUNT)
    with TemporaryDirectory(prefix="fertile-systemd-process.", dir="/tmp") as scratch:
        parent = Path(scratch)
        parent.chmod(0o755)
        root = parent / "instance"
        drill(root, prepare(root, account), account)


def fixture_payload():
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for relative in ("deploy", "scripts", "VERSION", "tests/systemd_process_test.py"):
            archive.add(REPO / relative, arcname=f"{Path(KIT).name}/{relative}",
                        filter=lambda member: None if "__pycache__" in Path(member.name).parts else member)
    return data.getvalue()


def validate_fixture():
    sys.path.insert(0, str(REPO / "scripts"))
    import systemd_units as units

    with TemporaryDirectory(prefix="fertile-systemd-validation.", dir="/tmp") as scratch:
        parent = Path(scratch)
        root = parent / "instance"
        control = parent / "control"
        (control / "bin").mkdir(parents=True)
        (control / "bin/python").symlink_to(sys.executable)
        data = prepare(root, pwd.getpwuid(os.getuid()), control)
        if units.selected_release(root) != COMMIT:
            raise RuntimeError("fixture lacks the selected release")
        generated = units.render(root, COMMIT, ACCOUNT, ACCOUNT)
        units.validate_units(generated)
        launcher = units.quote(root / "releases" / COMMIT / "fertile/scripts/systemd-service.sh")
        for service in SERVICES:
            body = generated[unit(service)]
            required = (
                f"User={ACCOUNT}\nGroup={ACCOUNT}",
                f"ExecCondition=:/bin/bash {launcher} condition {service}",
                f"ExecStartPre=:/bin/bash {launcher} prepare {service}",
                f"ExecStart=:/bin/bash {launcher} run {service}",
                f'"FERTILE_SYSTEMD_DATA_ROOT={data}"',
                "LimitNOFILE=4096",
            )
            if any(setting not in body for setting in required):
                raise RuntimeError(f"incomplete generated fixture unit for {service}")
            app = root / "current/bin" / service
            if not os.access(app, os.X_OK):
                raise RuntimeError(f"fixture binary missing for {service}")
            compile(app.read_text(), str(app), "exec")
    with tarfile.open(fileobj=io.BytesIO(fixture_payload())) as archive:
        archive.getmember(f"{Path(KIT).name}/tests/systemd_process_test.py")
    print("PASS synthetic release and production systemd unit fixture validation")


def docker_drill(image):
    # The container needs systemd's writable private cgroups. Keep its network,
    # PID namespace, filesystem and manager socket separate from the host.
    result = command(*DOCKER, "run", "--detach", "--pull=never", "--network=none", "--privileged", "--init=false",
                     "--cgroupns=private", "--tmpfs=/run:mode=0755",
                     "--tmpfs=/tmp:rw,exec,nosuid,nodev,mode=1777",
                     "--env", "FERTILE_DRILL_CALLER_PID_NAMESPACE=" + os.readlink("/proc/self/ns/pid"),
                     image, "/usr/lib/systemd/systemd", "--unit=basic.target", "--log-target=console",
                     stdout=subprocess.PIPE, text=True)
    container_id = result.stdout.strip()
    try:
        def manager_ready():
            probe = command(*DOCKER, "exec", container_id, *SYSTEMCTL, "show", "--property=Version",
                            timeout=5, check=False, capture_output=True, text=True)
            return probe.returncode == 0 and probe.stdout.startswith("Version=")

        wait_for(manager_ready, "isolated systemd manager", timeout=35)
        # Docker's copy API does not write into tmpfs mounts.
        command(*DOCKER, "exec", "-i", container_id, "tar", "-xf", "-", "-C", "/tmp",
                input=fixture_payload())
        command(*DOCKER, "exec", container_id, "/usr/bin/python3", "-B",
                f"{KIT}/tests/systemd_process_test.py", "--inside", timeout=100)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        command(*DOCKER, "logs", "--tail=60", container_id, check=False)
        raise
    finally:
        command(*DOCKER, "rm", "--force", container_id, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--image", help="prebuilt local systemd drill image")
    modes.add_argument("--validate-fixture", action="store_true", help="validate the fixture without launching systemd")
    modes.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        inside()
    elif args.validate_fixture:
        validate_fixture()
    else:
        docker_drill(args.image)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"systemd drill: {error}")
