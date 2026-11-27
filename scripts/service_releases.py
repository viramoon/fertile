"""Persistent per-service selections, scoped to an installed stack release."""
import json
import os
from pathlib import Path
import re
import stat
import tempfile


COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SERVICE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*\Z")


def release_path(root, commit):
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ValueError("expected a full 40-character built release commit")
    path = root / "releases" / commit
    if not path.is_dir() or path.resolve() != path:
        raise ValueError(f"release is missing or uses a symbolic link: {commit}")
    return path


def selections_path(root):
    return root / "config/service-releases.json"


def selections(root, base):
    path = selections_path(root)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("service release selection must be a regular file")
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    if not (isinstance(value, dict) and value.get("version") == 1
            and isinstance(value.get("base"), str) and COMMIT.fullmatch(value["base"])
            and isinstance(value.get("services"), dict)):
        raise ValueError("invalid service release selection")
    if value["base"] != base.name:
        return {}
    services = value["services"]
    for name, commit in services.items():
        if not SERVICE.fullmatch(name) or not isinstance(commit, str) or not COMMIT.fullmatch(commit):
            raise ValueError("invalid pinned service or commit")
    return services


def pinned(root, base, name):
    commit = selections(root, base).get(name)
    return release_path(root, commit) if commit else None


def selected(root, base, name):
    return pinned(root, base, name) or base


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def select(root, base, name, release):
    """Called with the shared control lock held after the incumbent has stopped."""
    if not SERVICE.fullmatch(name):
        raise ValueError("invalid service name")
    if any(release_path(root, path.name) != path for path in (base, release)):
        raise ValueError("service release must be inside this instance")
    services = {**selections(root, base), name: release.name}
    path = selections_path(root)
    if not path.parent.exists():
        path.parent.mkdir()
        sync_directory(root)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump({"version": 1, "base": base.name, "services": services}, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temp, path)
            sync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)
