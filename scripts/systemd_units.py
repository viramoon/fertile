"""Render and manage the system-wide Fertile units using only the standard library."""
import argparse
from contextlib import contextmanager
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
from string import Template
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = Path("/etc/systemd/system")
SYSTEMD_BOOT = Path("/run/systemd/system")
SERVICES = ("postgres", "finance-api", "finance-collector", "finance-backtest-worker", "web")
TARGET = "fertile.target"
PREFIX = "# fertile-systemd-v1 "
ACCOUNT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.-]*\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
SYSTEM_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}


def service_unit(name):
    if name not in SERVICES:
        raise ValueError(f"service unavailable in systemd mode: {name}")
    return f"fertile-{name}.service"


def installation_root(value):
    path = Path(value)
    if not path.is_absolute() or any(not char.isprintable() for char in str(path)):
        raise ValueError("FERTILE_ROOT must be a printable absolute path")
    root = path.resolve()
    if root == Path("/"):
        raise ValueError("FERTILE_ROOT cannot be /")
    return root


def quote(value):
    value = str(value)
    if any(not char.isprintable() for char in value):
        raise ValueError("systemd settings cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def release_path(root, commit):
    if not COMMIT.fullmatch(commit):
        raise ValueError("expected a built 40-character release commit")
    return root / "releases" / commit


def selected_release(root):
    current = root / "current"
    release = current.resolve(strict=True)
    if not current.is_symlink() or release != release_path(root, release.name) or not release.is_dir():
        raise ValueError("current must link to a built directory under releases/")
    for path in ("env.sh", "tools/supervisor/bin/python"):
        if not (root / path).is_file():
            raise ValueError(f"missing {path}; run bootstrap and scripts/setup-supervisor.sh as the service user")
    if not (release / "fertile/scripts/systemd-service.sh").is_file():
        raise ValueError("the current release needs systemd support; build the updated commit")
    return release.name


def render(root, commit, user, group):
    if not ACCOUNT.fullmatch(user) or not ACCOUNT.fullmatch(group) or "root" in (user, group):
        raise ValueError("systemd units require a non-root user and group name")
    release = release_path(root, commit)
    marker = PREFIX + json.dumps({"root": str(root), "release": commit}, sort_keys=True)
    templates = REPO / "deploy/systemd"
    service = Template((templates / "service.tmpl").read_text())
    target = Template((templates / "target.tmpl").read_text())
    launcher = quote(release / "fertile/scripts/systemd-service.sh")
    units = {}
    previous = None
    for name in SERVICES:
        dependencies = []
        if previous:
            dependencies.append(f"After={previous}")
        if name == "web":
            pg = service_unit("postgres")
            dependencies.extend([f"Wants={pg}", f"After={pg}"])
        # The ':' prefix disables systemd's environment expansion in command arguments.
        command = f":/bin/bash {launcher}"
        prepare = f"ExecStartPre={command} prepare {name}"
        ready = f"ExecStartPost={command} ready {name}" if name == "postgres" else ""
        units[service_unit(name)] = service.substitute(
            owner=marker, name=name, dependencies="\n".join(dependencies), user=user, group=group,
            environment=quote(f"FERTILE_ROOT={root}"), prepare=prepare, start=f"{command} run {name}",
            ready=ready, kill_mode="mixed" if name == "postgres" else "control-group",
            kill_signal="SIGINT" if name == "postgres" else "SIGTERM",
        )
        previous = service_unit(name)
    units[TARGET] = target.substitute(owner=marker, units=" ".join(units))
    return units


def validate_units(units):
    """Validate sections, links and required settings when systemd-analyze is unavailable."""
    expected = {*(service_unit(name) for name in SERVICES), TARGET}
    if units.keys() != expected:
        raise ValueError("incomplete set of systemd units")
    for unit, content in units.items():
        sections = {}
        section = None
        for line in content.splitlines():
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = sections.setdefault(line[1:-1], {})
                continue
            key, separator, value = line.partition("=")
            if section is None or not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) or not value:
                raise ValueError(f"{unit}: invalid directive: {line}")
            section.setdefault(key, []).append(value)
        metadata = sections["Unit"]
        required = {"Description"}
        if unit == TARGET:
            required |= {"Wants", "After"}
            if sections["Install"].get("WantedBy") != ["multi-user.target"]:
                raise ValueError("fertile.target must be installed with multi-user.target")
            for key in ("Wants", "After"):
                if set(metadata.get(key, [""])[0].split()) != expected - {TARGET}:
                    raise ValueError(f"{unit}: {key} must include all core services")
        else:
            fields = sections["Service"]
            if (fields.get("Type") != ["exec"] or fields.get("Restart") != ["always"] or
                any(not fields.get(key) for key in
                    ("User", "Group", "Environment", "ExecStart", "ExecStartPre", "TimeoutStopSec"))):
                raise ValueError(f"{unit}: incomplete service directives")
            if metadata.get("PartOf") != [TARGET]:
                raise ValueError(f"{unit}: missing reference to {TARGET}")
            if any(not entry.startswith(":/bin/bash ") for key in ("ExecStart", "ExecStartPre", "ExecStartPost")
                   for entry in fields.get(key, [])):
                raise ValueError(f"{unit}: malformed service command")
        if required - metadata.keys():
            raise ValueError(f"{unit}: incomplete unit directives")
        for key in ("After", "Wants"):
            if set(" ".join(metadata.get(key, [])).split()) - expected:
                raise ValueError(f"{unit}: unknown dependency")


def verify(units):
    validate_units(units)
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        print("systemd units verified with static syntax and required-field checks (systemd-analyze unavailable)")
        return
    with tempfile.TemporaryDirectory(prefix="fertile-systemd-") as directory:
        paths = []
        for name, content in units.items():
            path = Path(directory) / name
            path.write_text(content)
            paths.append(path)
        # Use an isolated manager scratch directory. Verification never needs
        # the host manager, its unit generators or its /run/systemd directory.
        env = {**os.environ, "LC_ALL": "C", "XDG_RUNTIME_DIR": directory, "SYSTEMD_UNIT_PATH": directory + ":"}
        result = subprocess.run([analyzer, "--user", "--generators=no", "verify", *paths],
                                env=env, text=True, capture_output=True)
        if result.returncode and result.stderr.strip() == "SO_PASSCRED failed: Operation not permitted":
            print("systemd units verified with static syntax and required-field checks "
                  "(systemd-analyze manager socket blocked by sandbox)")
            return
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        result.check_returncode()
    print("systemd units verified with systemd-analyze verify")


def systemctl(*args, **kwargs):
    return subprocess.run([SYSTEMCTL, "--system", "--no-pager", "--no-ask-password", *args],
                          env=SYSTEM_ENV, text=True, **kwargs)


def unit_state(*names):
    result = systemctl("show", "--all", "--property=Id,LoadState,FragmentPath,ActiveState,MainPID", *names,
                       capture_output=True)
    rows = {}
    for block in result.stdout.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Id"):
            rows[fields["Id"]] = fields
    if rows.keys() != set(names):
        raise RuntimeError(f"cannot inspect systemd units: {result.stderr.strip()}")
    if result.returncode and not any(row.get("LoadState") == "not-found" for row in rows.values()):
        raise RuntimeError(f"cannot inspect systemd units: {result.stderr.strip()}")
    return rows


def owner(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"expected a root-owned regular unit file: {path}")
        first_line = stream.readline(8192)
    if not first_line.startswith(PREFIX):
        raise ValueError(f"unit belongs to another deployment: {path}")
    marker = json.loads(first_line[len(PREFIX):])
    if not isinstance(marker, dict) or set(marker) != {"root", "release"}:
        raise ValueError(f"invalid Fertile unit marker: {path}")
    return marker


def matching_unit(path, root):
    marker = owner(path)
    if marker is not None and marker["root"] != str(root):
        raise ValueError(f"unit belongs to another Fertile root: {path}")
    return marker


def managed_release(root):
    try:
        marker = matching_unit(UNIT_DIR / TARGET, root)
    except (OSError, ValueError):
        # A foreign unit or a different root remains outside this controller.
        return None
    if marker is None or not SYSTEMD_BOOT.is_dir():
        return None
    target = unit_state(TARGET)[TARGET]
    if target["FragmentPath"] != str(UNIT_DIR / TARGET):
        raise RuntimeError("Fertile unit files exist, but systemd is not using them")
    return release_path(root, marker["release"])


@contextmanager
def lock_unit_directory():
    if os.geteuid() != 0:
        raise PermissionError("install-systemd and uninstall-systemd require root")
    if UNIT_DIR.resolve() != UNIT_DIR:
        raise ValueError("the systemd unit directory must not use symlinks")
    fd = os.open(UNIT_DIR, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("the systemd unit directory must be root-owned and not writable by other users")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def check_ownership(root, units):
    present = {name: marker for name in units
               if (marker := matching_unit(UNIT_DIR / name, root)) is not None}
    states = unit_state(*units)
    for name, state in states.items():
        fragment = state["FragmentPath"]
        if (fragment and (name not in present or fragment != str(UNIT_DIR / name))) or (
            not fragment and state["LoadState"] != "not-found"
        ):
            raise ValueError(f"systemd unit belongs to another deployment: {name}")
    return present, states


def write_unit(path, content):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".fertile-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def install(root, user, group):
    if pwd.getpwnam(user).pw_uid == 0 or grp.getgrnam(group).gr_gid == 0:
        raise ValueError("systemd services must use a non-root user and group")
    with lock_unit_directory():
        units = render(root, selected_release(root), user, group)
        present, states = check_ownership(root, units)
        changed = [name for name, content in units.items()
                   if name not in present or (UNIT_DIR / name).read_text() != content]
        if changed and any(row["ActiveState"] not in {"inactive", "failed"} for row in states.values()):
            raise RuntimeError(f"stop {TARGET} before replacing its unit files")
        for name in changed:
            write_unit(UNIT_DIR / name, units[name])
        systemctl("daemon-reload", check=True)
        systemctl("enable", TARGET, check=True)
    print(f"{TARGET} enabled; run systemctl start {TARGET} to launch it")


def uninstall(root):
    units = [TARGET, *(service_unit(name) for name in reversed(SERVICES))]
    with lock_unit_directory():
        present, states = check_ownership(root, units)
        loaded = [name for name in present if states[name]["FragmentPath"]]
        # Stop only owned units that the running manager has loaded. This also
        # handles a partial install where the target has not been written yet.
        if loaded:
            systemctl("stop", *loaded, check=True)
        if TARGET in present:
            systemctl("disable", TARGET, check=True)
        for name in present:
            (UNIT_DIR / name).unlink()
        systemctl("daemon-reload", check=True)


def service_command(root, action, name):
    release = managed_release(root)
    if release is None:
        raise RuntimeError("no Fertile systemd installation for this root")
    unit = service_unit(name)
    matching = matching_unit(UNIT_DIR / unit, root)
    if (matching is None or matching["release"] != release.name or
        unit_state(unit)[unit]["FragmentPath"] != str(UNIT_DIR / unit)):
        raise ValueError(f"service unavailable in systemd mode: {name}")
    if action == "restart":
        if os.path.lexists(root / "run/restore-journal.json"):
            raise RuntimeError("unfinished restore; run scripts/restore.sh before restarting services")
        systemctl("restart", unit, check=True)
    else:
        subprocess.run([JOURNALCTL, "--no-pager", "-n", "100", "-u", unit],
                       env=SYSTEM_ENV, check=True)


def main():
    parser = argparse.ArgumentParser(prog="fertile")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("install-systemd", "uninstall-systemd", "_managed", "_restart", "_logs"):
        child = commands.add_parser(action)
        child.add_argument("--root", required=True, type=installation_root)
        if action == "install-systemd":
            child.add_argument("--user", required=True)
            child.add_argument("--group", required=True)
        if action in ("_restart", "_logs"):
            child.add_argument("name")
    commands.add_parser("verify-systemd")
    args = parser.parse_args()
    if args.action == "verify-systemd":
        verify(render(Path("/var/lib/fertile"), "a" * 40, "fertile", "fertile"))
    elif args.action == "install-systemd":
        install(args.root, args.user, args.group)
    elif args.action == "uninstall-systemd":
        uninstall(args.root)
    elif args.action == "_managed":
        release = managed_release(args.root)
        if release is None:
            return 3
        print(release)
    else:
        service_command(args.root, args.action[1:], args.name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f"fertile: {error}")
