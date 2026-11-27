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
TARGET = "fertile.target"
PREFIX = "# fertile-systemd-v1 "
ACCOUNT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.-]*\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
FINDMNT = "/usr/bin/findmnt"
SYSTEM_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
STORAGE_KEYS = {"FERTILE_MARKET_SOURCE", "FERTILE_DATA_ROOT", "FERTILE_RESEARCH_ROOT"}
MOUNTINFO = Path("/proc/self/mountinfo")

# JSON is a YAML 1.2 subset. The root-owned installer reads only the trusted
# inventory and templates beside this script, using the system interpreter.
SYSTEMD_ROWS = {row["name"]: row for row in json.loads((REPO / "deploy/services.yaml").read_text())["services"]
                if row.get("systemd")}
SERVICES = tuple(name for name, row in SYSTEMD_ROWS.items() if "availability" not in row)


def service_unit(name):
    if name not in SYSTEMD_ROWS:
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


def host_rows(release, *, include_optional=False):
    for row in SYSTEMD_ROWS.values():
        if not row.get("enabled", True):
            continue
        if "availability" in row and not include_optional:
            artifact = Path(Template(row.get("availability_artifact", row["command"][0])).substitute(release=str(release)))
            if not artifact.is_file():
                continue
        yield row


def storage_settings(root):
    """Read only storage assignments from the public instance configuration."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except FileNotFoundError:
        return {}
    try:
        directory = os.open("config", flags, dir_fd=root_fd)
    except FileNotFoundError:
        return {}
    finally:
        os.close(root_fd)
    try:
        try:
            fd = os.open("runtime.env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("config/runtime.env must be a regular file")
            result = {}
            for line in stream:
                key, sep, value = line.strip().partition("=")
                if sep and key in STORAGE_KEYS:
                    if value.startswith(('"', "'")) and value.endswith(value[0]):
                        value = value[1:-1]
                    result[key] = value
            return result
    finally:
        os.close(directory)


def storage_roots(root, settings):
    source = settings.get("FERTILE_MARKET_SOURCE", "ib")
    if source not in {"ib", "replay"}:
        raise ValueError("unsupported market source")
    data = Path(settings.get("FERTILE_DATA_ROOT", root / ("data/replay" if source == "replay" else "data")))
    if not data.is_absolute():
        raise ValueError("FERTILE_DATA_ROOT must be absolute")
    research_base = root / "data/research"
    research = Path(settings.get("FERTILE_RESEARCH_ROOT", research_base))
    if (not (research.is_relative_to(research_base) or research == data / "research")
            or research.resolve() != research):
        raise ValueError("FERTILE_RESEARCH_ROOT must be a concrete path within fertile data/research or at FERTILE_DATA_ROOT/research")
    return data.resolve(), research


def mounted_paths():
    # mountinfo identifies bind mounts even when their device number matches the parent.
    return {Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
            for line in MOUNTINFO.read_text().splitlines()}


def declared_mounts():
    result = subprocess.run([FINDMNT, "--fstab", "--list", "--json", "--output", "TARGET"],
                            env=SYSTEM_ENV, text=True, capture_output=True)
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return set()
    result.check_returncode()
    entries = json.loads(result.stdout)["filesystems"] or []
    return {path.resolve() for row in entries if (path := Path(row["target"])).is_absolute()}


def expected_mounts(paths, data, root, mounts):
    expected = {mount for mount in mounts if mount != mount.parent
                and any(path.is_relative_to(mount) for path in paths)}
    if (data != data.parent and not data.is_relative_to(root)
            and any(path.is_relative_to(data) for path in paths)
            and not any(data.is_relative_to(mount) for mount in expected)):
        expected.add(data)
    return sorted(expected)


def validate_configured_roots(plan, name):
    """Reject mismatched runtime volumes or a missing mount before launching a service."""
    expected_data = os.environ.get("FERTILE_SYSTEMD_DATA_ROOT")
    expected_research = os.environ.get("FERTILE_SYSTEMD_RESEARCH_ROOT")
    if expected_data is not None or expected_research is not None:
        research = plan["services"][name]["env"]["FERTILE_RESEARCH_ROOT"]
        if (str(Path(plan["data"]).resolve()), str(Path(research).resolve())) != (expected_data, expected_research):
            raise ValueError("storage roots changed; stop fertile.target and reinstall systemd units")
    expected = json.loads(os.environ.get("FERTILE_SYSTEMD_MOUNTS", "[]"))
    if expected:
        missing = set(map(Path, expected)) - mounted_paths()
        if missing:
            raise RuntimeError(f"required systemd mount is unavailable: {min(missing)}")


def render(root, commit, user, group, *, include_optional=False, settings=None, mounts=None):
    if not ACCOUNT.fullmatch(user) or not ACCOUNT.fullmatch(group) or "root" in (user, group):
        raise ValueError("systemd units require a non-root user and group name")
    release = release_path(root, commit)
    marker = PREFIX + json.dumps({"root": str(root), "release": commit}, sort_keys=True)
    templates = REPO / "deploy/systemd"
    service = Template((templates / "service.tmpl").read_text())
    target = Template((templates / "target.tmpl").read_text())
    launcher = quote(release / "fertile/scripts/systemd-service.sh")
    units = {}
    settings = storage_settings(root) if settings is None else settings
    data, research = storage_roots(root, settings)
    if mounts is None:
        mounts = mounted_paths() | declared_mounts()
    fields = dict(root=root, release=release, data=data, research=research, run=root / "run", logs=root / "logs")
    for row in host_rows(release, include_optional=include_optional):
        name = row["name"]
        dependencies = []
        for key in ("after", "wants"):
            if names := row.get(key):
                dependencies.append(f"{key.capitalize()}=" + " ".join(map(service_unit, names)))
        paths = dict.fromkeys([root, *(Path(Template(path).substitute(fields)) for path in row.get("volumes", {}))])
        dependencies.append("RequiresMountsFor=" + " ".join(map(quote, paths)))
        expected = json.dumps([str(path) for path in expected_mounts(paths, data, root, mounts)])
        storage = "\n".join(f"Environment={quote(f'{key}={value}')}" for key, value in (
            ("FERTILE_SYSTEMD_DATA_ROOT", data),
            ("FERTILE_SYSTEMD_RESEARCH_ROOT", research),
            ("FERTILE_SYSTEMD_MOUNTS", expected),
        ))
        # The ':' prefix disables systemd's environment expansion in command arguments.
        command = f":/bin/bash {launcher}"
        prepare = f"ExecStartPre={command} prepare {name}"
        ready = f"ExecStartPost={command} ready {name}" if row.get("startup") == "postgres" else ""
        condition = f"ExecCondition={command} condition {name}"
        limits = row.get("limits", {})
        units[service_unit(name)] = service.substitute(
            owner=marker, name=name, dependencies="\n".join(dependencies), user=user, group=group,
            environment=quote(f"FERTILE_ROOT={root}"), storage=storage, prepare=prepare, start=f"{command} run {name}",
            condition=condition, ready=ready, stop_seconds=limits.get("stop_seconds", 30),
            nofile=limits.get("nofile", 1024), kill_mode="mixed" if name == "postgres" else "control-group",
            kill_signal="SIGINT" if name == "postgres" else "SIGTERM",
        )
    units[TARGET] = target.substitute(owner=marker, units=" ".join(units))
    return units


def validate_units(units):
    """Validate sections, links and required settings when systemd-analyze is unavailable."""
    expected = set(units)
    core = {*(service_unit(name) for name in SERVICES), TARGET}
    allowed = {*(service_unit(name) for name in SYSTEMD_ROWS), TARGET}
    if not core <= expected <= allowed:
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
            required |= {"Wants", "Before", "DefaultDependencies", "Conflicts"}
            if sections["Install"].get("WantedBy") != ["multi-user.target"]:
                raise ValueError("fertile.target must be installed with multi-user.target")
            members = expected - {TARGET}
            if set(" ".join(metadata.get("Wants", [])).split()) != members:
                raise ValueError(f"{unit}: Wants must include every generated service")
            # PartOf queues the member stop jobs. Reverse ordering makes
            # them finish before the target's own stop job completes.
            if (metadata.get("DefaultDependencies") != ["no"] or metadata.get("After")
                    or set(" ".join(metadata.get("Before", [])).split()) != members | {"shutdown.target"}
                    or metadata.get("Conflicts") != ["shutdown.target"]):
                raise ValueError(f"{unit}: invalid shutdown ordering for member services")
        else:
            fields = sections["Service"]
            if (fields.get("Type") != ["exec"] or fields.get("Restart") != ["always"] or
                any(not fields.get(key) for key in
                    ("User", "Group", "Environment", "ExecCondition", "ExecStart", "ExecStartPre",
                     "TimeoutStopSec", "LimitNOFILE"))):
                raise ValueError(f"{unit}: incomplete service directives")
            if metadata.get("PartOf") != [TARGET]:
                raise ValueError(f"{unit}: missing reference to {TARGET}")
            if any(not entry.startswith(":/bin/bash ") for key in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecCondition")
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
        obsolete = [service_unit(name) for name in SYSTEMD_ROWS
                    if service_unit(name) not in units and os.path.lexists(UNIT_DIR / service_unit(name))]
        present, states = check_ownership(root, [*units, *obsolete])
        changed = [name for name, content in units.items()
                   if name not in present or (UNIT_DIR / name).read_text() != content]
        if (changed or obsolete) and any(row["ActiveState"] not in {"inactive", "failed"} for row in states.values()):
            raise RuntimeError(f"stop {TARGET} before replacing its unit files")
        for name in changed:
            write_unit(UNIT_DIR / name, units[name])
        for name in obsolete:
            (UNIT_DIR / name).unlink()
        systemctl("daemon-reload", check=True)
        systemctl("enable", TARGET, check=True)
    print(f"{TARGET} enabled; run systemctl start {TARGET} to launch it")


def uninstall(root):
    units = [TARGET, *(service_unit(name) for name in reversed(SYSTEMD_ROWS))]
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
        verify(render(Path("/var/lib/fertile"), "a" * 40, "fertile", "fertile", include_optional=True,
                      settings={}, mounts=set()))
    elif args.action == "install-systemd":
        install(args.root, args.user, args.group)
    elif args.action == "uninstall-systemd":
        uninstall(args.root)
    elif args.action == "_managed":
        release = managed_release(args.root)
        if release is None:
            return 3
        print(release)
    elif args.action == "_restart":
        if os.geteuid() == 0:
            raise PermissionError("run fertile restart as the non-root service account")
        # The CLI uses Python isolated mode. Import only the controller
        # shipped alongside this trusted systemd entry point.
        sys.path.insert(0, str(REPO / "scripts"))
        from hot_upgrade import lock, require_clear

        with lock(args.root):
            require_clear(args.root)
            service_command(args.root, "restart", args.name)
    else:
        service_command(args.root, "logs", args.name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f"fertile: {error}")
