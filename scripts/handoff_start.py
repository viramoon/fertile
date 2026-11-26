"""Fence manager starts while a service's handoff is unresolved."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time

import hot_upgrade


START_STEPS = {"reader-cutover", "collector-start", "worker-start", "reconcile"}


def controller_identity(pid):
    try:
        # The command in /proc/PID/stat may contain spaces and parentheses.
        fields = (Path("/proc") / str(pid) / "stat").read_text().rpartition(")")[2].split()
        if fields[0] in {"Z", "X"}:
            return None
        return [Path("/proc/sys/kernel/random/boot_id").read_text().strip(), fields[19]]
    except (FileNotFoundError, ProcessLookupError):
        return None


@contextmanager
def open_permit(path):
    with os.fdopen(os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK), "r+") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("handoff start permit must be a regular file")
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield stream


def ticket_path(root, transaction_id):
    if not hot_upgrade.TRANSACTION_ID.fullmatch(transaction_id):
        raise ValueError("invalid handoff start transaction")
    return Path(root) / "run/handoff-start" / transaction_id


def ticket_scope(record, release, manager):
    return {"step": record["step"], "service": record["service"], "release": release, "manager": manager}


def consume(stream, permit):
    stream.seek(0)
    json.dump({**permit, "used": True}, stream)
    stream.truncate()
    stream.flush()


@contextmanager
def authorize(root, record, release, manager):
    """Open a single launch slot under the controller's existing control lock."""
    if (not hot_upgrade.unresolved(record) or record["step"] not in START_STEPS
            or record["step_state"] != "intent" or release not in {record["previous"], record["target"]}
            or (record["step"] != "reconcile" and release != record["target"])):
        raise ValueError("handoff step cannot authorize that service release")
    pid = os.getpid()
    permit = {**ticket_scope(record, release, manager), "controller": pid,
              "identity": controller_identity(pid), "used": False}
    if permit["identity"] is None:
        raise RuntimeError("could not identify the handoff controller")
    path = ticket_path(root, record["id"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(permit, stream)
            stream.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    try:
        yield path
    finally:
        # Invalidate an already-open descriptor before removing the name.
        with open_permit(path) as stream:
            consume(stream, permit)
            path.unlink()


def require_start(root, name, release, manager, *, launch=False):
    pending = [record for record in hot_upgrade.records(root)
               if hot_upgrade.unresolved(record) and record.get("service") == name]
    if not pending:
        return
    record = pending[0]
    path = ticket_path(root, record["id"])
    if len(pending) == 1 and record.get("step") in START_STEPS and record.get("step_state") == "intent":
        try:
            with open_permit(path) as stream:
                permit = json.load(stream)
                pid = permit.get("controller")
                root_scope = ticket_scope(record, release, manager)
                identity = permit.get("identity")
                if (permit.get("used") is False and type(pid) is int and pid > 1 and identity is not None
                        and all(permit.get(field) == value for field, value in root_scope.items())
                        and controller_identity(pid) == identity):
                    if launch:
                        consume(stream, permit)
                    return
        except FileNotFoundError:
            pass
    raise RuntimeError(f"unfinished hot-upgrade transaction {record['id']} blocks starting {name}; reconcile it first")


def wait_for_launch(path, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with open_permit(path) as stream:
            if json.load(stream)["used"]:
                return
        time.sleep(.05)
    raise RuntimeError("handoff service did not claim its launch permit")
