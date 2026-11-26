"""Serialize and journal service handoffs independently of the host manager."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import ctypes
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import uuid

from backup_wal import identifier, position


VERSION = 1
JOURNAL = Path("state/hot-upgrade")
TRANSACTION_ID = re.compile(r"[0-9a-f]{32}")
RELEASE = re.compile(r"[0-9a-f]{40}")
ROLES = {"reader", "projector", "collector", "worker"}
STATUSES = {"running", "complete", "aborted", "needs-reconcile", "reconciled"}
UNRESOLVED = {"running", "needs-reconcile"}
CONTINUITY = {"unknown", "ticks-restored", "bars-restored", "snapshot-only", "unrecoverable"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def require_parent(expected_parent):
    if (os.getppid() != expected_parent
            or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)
            or os.getppid() != expected_parent):
        raise RuntimeError("handoff child lost its controller")


def unresolved(record):
    return record["status"] in UNRESOLVED


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(root):
    run = Path(root) / "run"
    run.mkdir(parents=True, exist_ok=True)
    with (run / "control.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def records(root):
    """Return durable handoff records, including interrupted transactions."""
    result = []
    directory = Path(root) / JOURNAL
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError("hot-upgrade journal directory cannot be a symbolic link")
    if directory.exists() and not directory.is_dir():
        raise ValueError("hot-upgrade journal path is not a directory")
    for path in directory.glob("*.json"):
        if path.is_symlink() or not path.is_file() or not TRANSACTION_ID.fullmatch(path.stem):
            raise ValueError("invalid hot-upgrade journal entry")
        try:
            item = json.loads(path.read_bytes())
        except (UnicodeError, ValueError) as error:
            raise ValueError("malformed hot-upgrade journal") from error
        if (not isinstance(item, dict) or type(item.get("version")) is not int or item["version"] != VERSION
                or item.get("id") != path.stem or not isinstance(item.get("status"), str)
                or item["status"] not in STATUSES
                or not isinstance(item.get("started_at"), str)):
            raise ValueError("invalid hot-upgrade journal version or state")
        result.append(item)
    return sorted(result, key=lambda item: (item["started_at"], item["id"]))


def _require_no_restore(root):
    if (Path(root) / "run/restore-journal.json").exists():
        raise RuntimeError("unfinished restore; reconcile the restore before upgrading")


def require_clear(root):
    """Call under the control lock before changing selected services."""
    _require_no_restore(root)
    pending = [record["id"] for record in records(root) if unresolved(record)]
    if pending:
        raise RuntimeError(f"unfinished hot-upgrade transaction {pending[0]}; reconcile it first")


def _ack(result, field):
    if result.get(field) is not True:
        raise ValueError(f"hook did not confirm {field}")
    return {field: True}


def _watermark(result, *, source=None, required=False):
    entries = result.get("watermark")
    if not isinstance(entries, dict) or (required and not entries):
        raise ValueError("hook did not supply required watermarks")
    for stream, offset in entries.items():
        if not isinstance(stream, str):
            raise ValueError("invalid watermark stream")
        stream_source, _, product = stream.partition("/")
        if not identifier(stream_source) or not identifier(product) or (source and stream_source != source):
            raise ValueError("invalid watermark stream")
        position(offset)
    return entries


def _at_least(observed, required):
    if any(key not in observed or position(observed[key]) < position(value) for key, value in required.items()):
        raise ValueError("candidate watermarks lag behind the handoff checkpoint")


class Transaction:
    def __init__(self, root, record, invoke):
        self.root = Path(root)
        self.record = record
        self.invoke = invoke

    def save(self):
        directory = self.root / JOURNAL
        for parent in (directory.parent, directory):
            if not parent.exists():
                parent.mkdir(mode=0o700)
                _sync_directory(parent.parent)
        self.record["updated_at"] = timestamp()
        with tempfile.NamedTemporaryFile(dir=directory, mode="w", encoding="utf-8", delete=False) as stream:
            path = Path(stream.name)
            try:
                json.dump(self.record, stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(path, directory / (self.record["id"] + ".json"))
                _sync_directory(directory)
            finally:
                path.unlink(missing_ok=True)

    def call(self, step, validate=None):
        self.record.update(step=step, step_state="intent")
        self.save()
        result = self.invoke(step, deepcopy(self.record))
        if not isinstance(result, dict):
            raise ValueError("handoff hook must return a JSON object")
        accepted = validate(result) if validate else {}
        self.record["evidence"][step] = accepted
        self.record["step_state"] = "confirmed"
        self.save()
        return accepted

    def reader(self):
        required = self.record["role"] == "projector"
        checkpoint = self.call("reader-checkpoint", lambda result: {
            "watermark": _watermark(result, required=required),
        })["watermark"]

        def ready(result):
            _ack(result, "ready")
            watermark = _watermark(result, required=required)
            _at_least(watermark, checkpoint)
            return {"ready": True, "watermark": watermark}

        self.call("candidate-start")
        self.call("candidate-ready", ready)
        self.call("reader-cutover")
        self.call("reader-ready", ready)

    def collector(self):
        source = self.record["source"]
        gap = self.record["gap"] = {
            "source": source,
            "opened_at": timestamp(),
            "ready_at": None,
            "continuity": "unknown",
            "missing_count": None,
        }

        def seal(result):
            watermark = _watermark(result, source=source)
            if not watermark and result.get("display_only") is not True:
                raise ValueError("collector seal needs watermarks or a display-only confirmation")
            return {"watermark": watermark, "display_only": not watermark}

        def checkpoint(result, flag, expected):
            _ack(result, flag)
            watermark = _watermark(result, source=source)
            if not sealed and watermark:
                raise ValueError("display-only collector reported a persistent watermark")
            _at_least(watermark, expected)
            return {flag: True, "watermark": watermark}

        sealed = self.call("collector-seal", seal)["watermark"]
        final = self.call("collector-release", lambda result: checkpoint(result, "released", sealed))["watermark"]
        self.call("collector-start")
        self.call("collector-ready", lambda result: checkpoint(result, "ready", final))
        gap["ready_at"] = timestamp()

        def classify(result):
            continuity = result.get("continuity")
            if not isinstance(continuity, str) or continuity not in CONTINUITY:
                raise ValueError("collector gap needs a continuity classification")
            count = result.get("missing_count")
            if count is not None:
                position(count)
            return {"continuity": continuity, "missing_count": count}

        gap.update(self.call("collector-gap", classify))

    def worker(self):
        self.call("worker-drain", lambda result: _ack(result, "drained"))
        self.call("worker-stop")
        self.call("worker-start")
        self.call("worker-ready", lambda result: _ack(result, "ready"))

    def run(self):
        step = None
        try:
            self.call("compatibility", lambda result: _ack(result, "compatible"))
            if self.record["migrate"]:
                self.call("migrate", lambda result: _ack(result, "expanded"))
            match self.record["role"]:
                case "reader" | "projector":
                    self.reader()
                case "collector":
                    self.collector()
                case "worker":
                    self.worker()
        except Exception:
            step = self.record["step"]
            self.record["failed_step"] = step
            if step in {"compatibility", "reader-checkpoint"}:
                self.record["status"] = "aborted"
            elif step in {"candidate-start", "candidate-ready"}:
                try:
                    self.call("candidate-abort")
                except Exception:
                    self.record["status"] = "needs-reconcile"
                else:
                    self.record["status"] = "aborted"
            else:
                self.record["status"] = "needs-reconcile"
        else:
            self.record["status"] = "complete"
        self.save()
        if step is not None:
            raise RuntimeError(f"hot-upgrade transaction {self.record['id']} failed at {step}")
        return self.record


def perform(root, invoke, *, service, role, previous, target, source=None, direction="upgrade", migrate=False,
            lock_held=False):
    """Run a handoff; the hook must enforce its manager/application contracts."""
    if (not identifier(service) or role not in ROLES or (role == "collector") != (source is not None)
            or source is not None and not identifier(source)):
        raise ValueError("invalid hot-upgrade service, role, or source")
    if (not isinstance(previous, str) or not isinstance(target, str) or not RELEASE.fullmatch(previous)
            or not RELEASE.fullmatch(target) or previous == target):
        raise ValueError("hot-upgrade requires distinct full release commits")
    if direction not in {"upgrade", "rollback"} or type(migrate) is not bool or (migrate and direction == "rollback"):
        raise ValueError("invalid hot-upgrade direction or migration")
    with nullcontext() if lock_held else lock(root):
        require_clear(root)
        record = dict(version=VERSION, id=uuid.uuid4().hex, service=service, role=role, source=source,
                      previous=previous, target=target, direction=direction, migrate=migrate, step=None,
                      status="running", started_at=timestamp(), evidence={}, gap=None)
        return Transaction(root, record, invoke).run()


def reconcile(root, transaction_id, invoke, *, lock_held=False):
    """Close an interrupted handoff after the adapter proves safe ownership."""
    if not isinstance(transaction_id, str) or not TRANSACTION_ID.fullmatch(transaction_id):
        raise ValueError("invalid hot-upgrade transaction identifier")
    with nullcontext() if lock_held else lock(root):
        _require_no_restore(root)
        record = next((item for item in records(root) if item["id"] == transaction_id), None)
        if record is None or not unresolved(record):
            raise ValueError("no unresolved hot-upgrade transaction with that identifier")
        transaction = Transaction(root, record, invoke)

        def verify(result):
            _ack(result, "reconciled")
            active = result.get("active_release")
            if active not in {record["previous"], record["target"]}:
                raise ValueError("reconciliation must identify the active release")
            return {"reconciled": True, "active_release": active}

        if "interrupted_step" not in record:
            record["interrupted_step"] = record["step"]
        try:
            transaction.call("reconcile", verify)
        except Exception:
            record["status"] = "needs-reconcile"
            transaction.save()
            raise RuntimeError(f"hot-upgrade transaction {transaction_id} could not be reconciled") from None
        record["status"] = "reconciled"
        transaction.save()
        return record


if __name__ == "__main__":
    parent, hook, step = sys.argv[1:]
    require_parent(int(parent))
    os.execv(hook, [hook, step])
