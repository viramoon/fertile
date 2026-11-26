"""Verified full and incremental recovery points on an rclone crypt remote."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

from backup import (ALLOWED_TREES, STAMP, Box, atomic_json, backup_settings, capture_snapshot, digest,
                    locked, overlaps, resolve_root, restore, restore_session, snapshots, sync_dir,
                    sync_tree, utc_stamp, verify, verify_restore)
from backup_crypt import CryptError, CryptRemote

UTC = timezone.utc
FORMAT = 1
REQUIRED = {"postgres.dump", "snapshot.json", "SHA256SUMS"}
MANAGED = ALLOWED_TREES | {"wal"}
SHA256 = re.compile(r"[a-f0-9]{64}")
RESTORE_MAX_AGE = timedelta(days=7)
RPO = timedelta(hours=1)
POINT_READ_ERRORS = {"remote_integrity", "remote_missing"}


class RecoveryError(ValueError):
    pass


class UnavailablePoint(RecoveryError):
    def __init__(self, point_id, message):
        super().__init__(message)
        self.point_id = point_id


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def point_time(point):
    dt = datetime.strptime(point, STAMP).replace(tzinfo=UTC)
    require(dt.strftime(STAMP) == point, "invalid recovery point identifier")
    return dt


def iso_point(point):
    return point_time(point).isoformat().replace("+00:00", "Z") if point else None


def object_names(files):
    objects = {}
    for entry in files.values():
        sha, size = entry["sha256"], entry["bytes"]
        require(objects.get(sha, size) == size, "inconsistent recovery object length")
        objects[sha] = size
    return objects


def safe_member(name):
    path = PurePosixPath(name)
    return (path.as_posix() == name and ".." not in path.parts
            and not any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in name)
            and (name in REQUIRED or len(path.parts) > 1 and path.parts[0] in MANAGED))


@dataclass(frozen=True)
class Point:
    id: str
    sha256: str
    kind: str
    parent: dict | None
    files: dict
    deleted: list
    snapshot: dict

    @classmethod
    def read(cls, path, point_id):
        point_time(point_id)
        data = path.read_bytes()
        doc = json.loads(data)
        require(doc["format"] == FORMAT and doc["id"] == point_id, "unknown recovery manifest format")
        kind, parent, files, deleted, snapshot = (doc[key] for key in ("kind", "parent", "files", "deleted", "snapshot"))
        require(kind in {"full", "hourly"} and isinstance(files, dict) and isinstance(deleted, list),
                "invalid recovery manifest")
        require(isinstance(snapshot, dict) and snapshot.get("stamp") == point_id
                and snapshot.get("format") == 1 and snapshot.get("has_database") is True, "invalid snapshot metadata")
        if kind == "full":
            require(parent is None and not deleted and REQUIRED <= files.keys(), "full recovery point has dependencies")
        else:
            require(isinstance(parent, dict) and SHA256.fullmatch(parent["sha256"]) and parent["id"] < point_id,
                    "invalid recovery parent")
            point_time(parent["id"])
        for name, info in files.items():
            require(safe_member(name) and isinstance(info, dict) and SHA256.fullmatch(info["sha256"])
                    and type(info["bytes"]) is int and info["bytes"] >= 0, "invalid recovery object")
        require(all(isinstance(name, str) and safe_member(name) and name not in files and name not in REQUIRED
                    for name in deleted) and len(deleted) == len(set(deleted)), "invalid recovery deletion")
        return cls(point_id, hashlib.sha256(data).hexdigest(), kind, parent, files, deleted, snapshot)

    def reference(self):
        return {"id": self.id, "sha256": self.sha256}

    def objects(self):
        return object_names(self.files)


@dataclass
class Inventory:
    points: dict
    invalid: set
    objects: set

    def chain(self, point_id):
        require(point_id in self.points, "recovery point missing or chain is broken")
        chain = []
        while point_id is not None:
            point = self.points[point_id]
            chain.append(point)
            point_id = point.parent["id"] if point.parent else None
        return chain[::-1]

    def latest(self, kind=None):
        return max((point.id for point in self.points.values() if kind is None or point.kind == kind), default=None)

    def newest_first(self):
        return (self.points[point_id] for point_id in sorted(self.points, reverse=True))


def exact_chain(remote, point_id, work):
    point_time(point_id)
    chain = []
    parent = None
    while point_id is not None:
        path = work / (point_id + ".json")
        try:
            remote.download(f"commits/{point_id}.json", path)
            point = Point.read(path, point_id)
        except CryptError as error:
            if error.code not in POINT_READ_ERRORS:
                raise
            raise RecoveryError("recovery point missing or chain is broken") from error
        except (ValueError, TypeError, KeyError) as error:
            raise RecoveryError("recovery point missing or chain is broken") from error
        require(parent is None or point.reference() == parent, "broken recovery chain")
        chain.append(point)
        parent = point.parent
        point_id = parent["id"] if parent else None
    return chain[::-1]


def inventory(remote, work):
    commits = work / "commits"
    commits.mkdir()
    listed = remote.list_files("commits")
    if listed:
        try:
            remote.download_tree("commits", commits)
        except CryptError as error:
            if error.code not in POINT_READ_ERRORS:
                raise
            # rclone may have copied other manifests before finding a bad one.
            # The point loop retries any missing file individually.
    objects = remote.list_files("objects")
    by_point = {}
    for name, size in objects.items():
        point, sep, sha = name.partition("/")
        if sep:
            by_point.setdefault(point, {})[sha] = size
    points, invalid = {}, set()
    for filename, size in sorted(listed.items()):
        point_id = filename.removesuffix(".json")
        try:
            require(filename == point_id + ".json" and "/" not in filename, "invalid commit name")
            point_time(point_id)
            path = commits / filename
            if not path.is_file():
                remote.download(f"commits/{filename}", path)
            require(path.is_file() and path.stat().st_size == size, "commit download incomplete")
            point = Point.read(path, point_id)
            require(by_point.get(point.id, {}) == point.objects(), "committed object inventory mismatch")
            if point.parent:
                parent = points.get(point.parent["id"])
                require(parent and parent.reference() == point.parent, "broken recovery chain")
            points[point.id] = point
        except CryptError as error:
            if error.code not in POINT_READ_ERRORS:
                raise
            invalid.add(point_id)
        except (ValueError, TypeError, KeyError):
            invalid.add(point_id)
    return Inventory(points, invalid, set(by_point))


def state_path(root):
    return root / "run/backup-remote-status.json"


def reference_id(name):
    return hashlib.sha256(name.encode()).hexdigest()


def read_state(root, name):
    path = state_path(root)
    try:
        state = json.loads(path.read_bytes())
        if not isinstance(state, dict) or state.get("remote_id") != reference_id(name):
            return {}
        for key in ("last_point", "full", "hourly"):
            if state.get(key) is not None:
                point_time(state[key])
        require(type(state.get("degraded", False)) is bool, "invalid backup status")
    except FileNotFoundError:
        return {}
    except (ValueError, TypeError):
        return {"degraded": True}
    return state


def update_state(root, name, *, published=None, degraded=None):
    with locked(root / "run/backup-remote-state.lock"):
        state = read_state(root, name)
        state["remote_id"] = reference_id(name)
        if published:
            for key in ("last_point", published.kind):
                state[key] = max(published.id, state.get(key) or "")
        if degraded is not None:
            state["degraded"] = degraded
        atomic_json(state_path(root), state)
    return state


def record_observation(root, name, found, heads, before):
    with locked(root / "run/backup-remote-state.lock"):
        current = read_state(root, name)
        state = {"remote_id": reference_id(name)}
        lost = False
        for kind in ("full", "hourly"):
            previous, cached = before.get(kind), current.get(kind)
            lost |= previous is not None and previous not in found.points
            # A full may finish publishing after the hourly listing. Preserve
            # that new observation unless this scan already disproved it.
            concurrent = cached if cached != previous and cached not in found.invalid else None
            state[kind] = max(heads.get(kind) or "", concurrent or "") or None
        state["last_point"] = max(state["full"] or "", state["hourly"] or "") or None
        state["degraded"] = bool(found.invalid or lost or not state["last_point"] and current.get("degraded"))
        atomic_json(state_path(root), state)
    return state

def status_fields(state, now):
    latest = state.get("last_point")
    age = now - point_time(latest) if latest else None
    degraded = state.get("degraded", False)
    return {"last_recoverable_at": iso_point(latest), "last_recoverable_point": latest,
            "last_full_at": iso_point(state.get("full")), "last_hourly_at": iso_point(state.get("hourly")),
            "age_seconds": int(age.total_seconds()) if age is not None else None,
            "alert": age is None or age < timedelta(0) or age > RPO or degraded, "degraded": degraded}


def configured_remote(root, settings):
    name = settings.get("FERTILE_BACKUP_REMOTE", "")
    require(name, "configure FERTILE_BACKUP_REMOTE to enable encrypted backup")
    secret_dir = root / "secrets"
    try:
        info = secret_dir.lstat()
    except FileNotFoundError:
        raise CryptError("key_missing", "backup secrets directory missing") from None
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not (info.st_mode & 0o077),
            "backup secrets directory must be owned by the service user and private")
    binary = settings.get("FERTILE_BACKUP_RCLONE", "/usr/bin/rclone")
    require(Path(binary).is_absolute(), "FERTILE_BACKUP_RCLONE must be absolute")
    return CryptRemote(binary, secret_dir / "backup-rclone.conf", name)


def file_map(snapshot):
    manifest = snapshot / "SHA256SUMS"
    entries = [line.split("  ", 1) for line in manifest.read_text().removesuffix("\n").split("\n")]
    entries.append((digest(manifest), manifest.name))
    return {name: {"sha256": sha, "bytes": (snapshot / name).stat().st_size} for sha, name in entries}


def effective(chain):
    files = {}
    for point in chain:
        for name in point.deleted:
            require(name in files, "recovery deletion has no predecessor")
            del files[name]
        files.update(point.files)
    return files


def payload_available(remote, point):
    expected = point.objects()
    if not expected:
        return True
    try:
        return remote.object_hashes("objects/" + point.id) == {name: name for name in expected}
    except CryptError as error:
        if error.code not in POINT_READ_ERRORS:
            raise
        return False


def verified_chain(remote, found, point, checked):
    chain = found.chain(point.id)
    for item in chain:
        if item.id in found.invalid:
            return None
        if item.id not in checked:
            if not payload_available(remote, item):
                found.invalid.add(item.id)
                return None
            checked.add(item.id)
    return chain


def verified_heads(remote, found, checked):
    heads = {}
    for point in found.newest_first():
        if point.kind not in heads and verified_chain(remote, found, point, checked):
            heads[point.kind] = point.id
        if len(heads) == 2:
            break
    return heads


def hour_parent(remote, found, point_id, checked):
    base = next((point.id for point in found.newest_first() if point.kind == "full"
                 and point.id < point_id and verified_chain(remote, found, point, checked)), None)
    if base is None:
        return None
    for point in found.newest_first():
        if point.id < point_id and found.chain(point.id)[0].id == base:
            if chain := verified_chain(remote, found, point, checked):
                return chain


def check_objects(directory, expected):
    entries = list(directory.iterdir())
    actual = {p.name: p.stat().st_size for p in entries if p.is_file() and not p.is_symlink()}
    require(actual == expected and len(entries) == len(expected), "remote recovery object count or size mismatch")
    require(all(digest(directory / name) == name for name in expected), "remote recovery object SHA256 mismatch")


def ciphertext_size(length):
    # rclone crypt uses a 32-byte header and adds a 16-byte tag per 64 KiB.
    return 32 + length + 16 * ((length + 65535) // 65536)


def publish(remote, work, snapshot, meta, kind, chain=()):
    parent = chain[-1] if chain else None
    current = file_map(snapshot)
    before = effective(chain)
    changed = {name: value for name, value in current.items() if before.get(name) != value}
    commit = work / "manifest.json"
    point_id = meta["stamp"]
    atomic_json(commit, dict(format=FORMAT, id=point_id, kind=kind, parent=parent.reference() if parent else None,
                             snapshot=meta, files=changed, deleted=sorted(before.keys() - current.keys())))
    point = Point.read(commit, point_id)
    expected = point.objects()
    remote.capacity(sum(ciphertext_size(size) for size in expected.values()) + ciphertext_size(commit.stat().st_size))
    outgoing = work / "outgoing"
    outgoing.mkdir()
    for name, info in changed.items():
        dest = outgoing / info["sha256"]
        if not dest.exists():
            os.link(snapshot / name, dest)
    received = work / "received"
    received.mkdir()
    target = "objects/" + point_id
    if expected:
        remote.upload_tree(outgoing, target)
        remote.download_tree(target, received)
    check_objects(received, expected)
    require(remote.list_files(target) == expected, "remote recovery object inventory mismatch")
    target = f"commits/{point_id}.json"
    # A write can be acknowledged remotely even if the command lost its response.
    try:
        remote.upload(commit, target)
    except CryptError as error:
        try:
            remote.download(target, work / "published.json")
        except CryptError:
            raise error from None
    else:
        remote.download(target, work / "published.json")
    require(digest(work / "published.json") == point.sha256, "published recovery manifest differs")
    return point


def materialize(remote, chain, snapshot):
    snapshot.mkdir(mode=0o700)
    for point in chain:
        received = Path(tempfile.mkdtemp(dir=snapshot.parent))
        try:
            if point.files:
                try:
                    remote.download_tree("objects/" + point.id, received)
                except CryptError as error:
                    if error.code in POINT_READ_ERRORS:
                        raise UnavailablePoint(point.id, "recovery payload is unreadable") from error
                    raise
            try:
                check_objects(received, point.objects())
            except RecoveryError as error:
                raise UnavailablePoint(point.id, str(error)) from error
            stale = set(point.deleted) | set(point.files)
            for name in sorted(stale):
                path = snapshot / name
                if path.is_file():
                    path.unlink()
                    for parent in path.parents:
                        if parent == snapshot or any(parent.iterdir()):
                            break
                        parent.rmdir()
                else:
                    if name in point.deleted:
                        raise UnavailablePoint(point.id, "recovery deletion has no predecessor")
            for name, info in point.files.items():
                path = snapshot / name
                path.parent.mkdir(parents=True, exist_ok=True)
                os.link(received / info["sha256"], path)
        finally:
            shutil.rmtree(received)
    expected = chain[-1].snapshot
    try:
        for tree in expected["trees"]:
            require(tree in ALLOWED_TREES, "unknown snapshot tree")
            (snapshot / tree).mkdir(exist_ok=True)
        if "wal" in expected:
            for tree in ("manifests", "segments"):
                (snapshot / "wal" / tree).mkdir(parents=True, exist_ok=True)
        require(verify(snapshot) == expected, "recovered snapshot differs from committed metadata")
    except (ValueError, KeyError, TypeError) as error:
        raise UnavailablePoint(chain[-1].id, "invalid recovered snapshot") from error
    return snapshot


def stage_restore(remote, selection, work, snapshot):
    if selection != "latest":
        chain = exact_chain(remote, selection, work)
        materialize(remote, chain, snapshot)
        return chain[-1]

    found = inventory(remote, work)
    for point in found.newest_first():
        chain = found.chain(point.id)
        if any(item.id in found.invalid for item in chain):
            continue
        try:
            materialize(remote, chain, snapshot)
        except UnavailablePoint as error:
            found.invalid.add(error.point_id)
            shutil.rmtree(snapshot)
        else:
            return point
    raise RecoveryError("no readable recovery point with an intact chain")


def keep_points(found, days, weeks, restored):
    daily, weekly, keep, daily_fulls = set(), set(), set(), set()
    for point in sorted(found.points.values(), key=lambda p: p.id, reverse=True):
        if point.kind != "full":
            continue
        date = point_time(point.id).date()
        week = date.isocalendar()[:2]
        if date not in daily and len(daily) < days:
            daily_fulls.add(point.id)
            daily.add(date)
        if week not in weekly and len(weekly) < weeks:
            keep.add(point.id)
            weekly.add(week)
    keep.update(daily_fulls)
    # Keep hours anchored to any full on a retained date, including a retry
    # that completed later with a second full on the same UTC date.
    for point in found.points.values():
        if point.kind == "hourly":
            chain = found.chain(point.id)
            if point_time(chain[0].id).date() in daily:
                keep.update(item.id for item in chain)
    for point_id in (found.latest(), restored):
        keep.update(item.id for item in found.chain(point_id))
    return keep


def receipt_file(root):
    return root / "run/backup-remote-restore.json"


def prune_remote(remote, root, name, work, settings, *, automatic=False):
    receipt = receipt_file(root)
    proof = json.loads(receipt.read_bytes()) if receipt.is_file() else {}
    recent = proof.get("remote_id") == reference_id(name) and (
        timedelta(0) <= datetime.now(UTC) - point_time(proof["restored_at"]) <= RESTORE_MAX_AGE)
    if automatic and not recent:
        return
    require(recent, "remote prune requires a successful isolated restore within seven days")
    found = inventory(remote, work)
    require(not found.invalid, "remote prune blocked by invalid recovery points")
    restored = found.points.get(proof["point"]["id"])
    require(restored and restored.reference() == proof["point"], "restored recovery point no longer verified")
    days = int(settings.get("FERTILE_BACKUP_DAYS", 7))
    weeks = int(settings.get("FERTILE_BACKUP_WEEKS", 4))
    require(days >= 1 and weeks >= 0, "retention requires days>=1 and weeks>=0")
    keep = keep_points(found, days, weeks, restored.id)
    # The retention set includes all ancestors. Check each point's current
    # content once, before deleting any commit or object.
    for point_id in sorted(keep):
        require(payload_available(remote, found.points[point_id]),
                "remote prune blocked by unreadable or corrupt retained recovery payload")

    obsolete = set(found.points) - keep
    for point_id in sorted(obsolete, reverse=True):
        remote.delete(f"commits/{point_id}.json")
    for point_id in sorted(found.objects - keep):
        try:
            instant = point_time(point_id)
        except ValueError:
            continue
        if point_id in obsolete or datetime.now(UTC) - instant > RESTORE_MAX_AGE:
            remote.purge("objects/" + point_id)
    print(json.dumps({"pruned": len(obsolete), "retained": len(keep)}))


def run_backup(args, root, name, remote, work):
    kind = args.action
    with locked(root / f"run/backup-remote-{kind}.lock"), locked(root / "run/backup-remote.lock", fcntl.LOCK_SH):
        snapshot = work / "snapshot"
        snapshot.mkdir()
        with restore_session(root) as fd:
            box = Box(quiet=True)
            box.fds = [fd]
            previous = next(snapshots(box.backups), None) if box.backups.exists() else None
            meta = capture_snapshot(box, snapshot, previous, remote=True, seal_wal=args.seal_wal)
        chain = ()
        if kind == "hourly":
            before = read_state(root, name)
            try:
                found = inventory(remote, work)
                checked = set()
                heads = verified_heads(remote, found, checked)
                chain = hour_parent(remote, found, meta["stamp"], checked)
            except CryptError:
                update_state(root, name, degraded=True)
                raise
            record_observation(root, name, found, heads, before)
            require(chain, "hourly backup requires a verified full recovery point")
        point = publish(remote, work, snapshot, meta, kind, chain)
        state = update_state(root, name, published=point)
    print(json.dumps({"point": point.id, "kind": kind, **status_fields(state, datetime.now(UTC))}))


def run_restore(args, root, name, remote, work):
    with locked(root / "run/backup-remote-restore.lock"), locked(root / "run/backup-remote.lock", fcntl.LOCK_SH):
        if args.snapshot_to is not None:
            target = args.snapshot_to
            require(target.is_absolute() and target == target.resolve() and target.parent.is_dir()
                    and not target.exists(), "snapshot destination must be an absent absolute path")
            with tempfile.TemporaryDirectory(dir=target.parent, prefix=".backup-receive-") as temporary:
                stage = Path(temporary) / "snapshot"
                point = stage_restore(remote, args.point, work, stage)
                sync_tree(stage)
                stage.rename(target)
                sync_dir(target.parent)
            print(json.dumps({"point": point.id, "snapshot": str(target)}))
            return
        targets = (args.target_data_root, args.target_database, None)
        stage = work / "snapshot"
        point = stage_restore(remote, args.point, work, stage)
        stage = stage.rename(work / point.id)
        with restore_session(root, targets, quiet=True) as fd:
            box = Box(restoring=True, quiet=True)
            box.fds = [fd]
            require(args.target_database != box.db and not overlaps(args.target_data_root.resolve(), box.data),
                    "remote restore requires a data root and database separate from the active instance")
            meta = verify_restore(box, stage, targets)
            restore(box, stage, meta, False, targets)
            atomic_json(receipt_file(root), {"remote_id": reference_id(name), "point": point.reference(),
                                            "restored_at": utc_stamp()})


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for command in ("full", "hourly"):
        commands.add_parser(command).add_argument("--seal-wal", action="store_true")
    commands.add_parser("status").add_argument("--cached", action="store_true")
    commands.add_parser("prune")
    recovery = commands.add_parser("restore")
    recovery.add_argument("point", help="exact recovery point identifier or latest")
    recovery.add_argument("--snapshot-to", type=Path)
    recovery.add_argument("--target-data-root", type=Path)
    recovery.add_argument("--target-database")
    args = parser.parse_args()
    if args.action == "restore":
        if args.snapshot_to is not None:
            if args.target_data_root is not None or args.target_database is not None:
                recovery.error("--snapshot-to is exclusive with database and data roots")
        elif args.target_data_root is None or args.target_database is None:
            recovery.error("specify --snapshot-to or both --target-data-root and --target-database")
    return args


def main():
    args = parse_args()
    os.umask(0o077)
    name = ""
    try:
        root = resolve_root().resolve(strict=True)
        settings = backup_settings(root)
        name = settings.get("FERTILE_BACKUP_REMOTE", "")
        if args.action == "status" and args.cached:
            print(json.dumps(status_fields(read_state(root, name), datetime.now(UTC))))
            return
        remote = configured_remote(root, settings)
        backups = root / "backups"
        require(backups == backups.resolve(), "unsafe backup staging directory")
        backups.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".remote-", dir=backups) as temporary:
            work = Path(temporary)
            if args.action in {"full", "hourly"}:
                run_backup(args, root, name, remote, work)
            elif args.action == "restore":
                run_restore(args, root, name, remote, work)
            else:
                with locked(root / "run/backup-remote.lock"):
                    if args.action == "status":
                        before = read_state(root, name)
                        found = inventory(remote, work)
                        heads = verified_heads(remote, found, set())
                        state = record_observation(root, name, found, heads, before)
                        print(json.dumps(status_fields(state, datetime.now(UTC))))
                    else:
                        prune_remote(remote, root, name, work, settings)
        if args.action == "full":
            try:
                with locked(root / "run/backup-remote.lock", fcntl.LOCK_EX | fcntl.LOCK_NB):
                    with tempfile.TemporaryDirectory(prefix=".remote-", dir=backups) as temporary:
                        prune_remote(remote, root, name, Path(temporary), settings, automatic=True)
            except BlockingIOError:
                pass
            except (CryptError, ValueError, OSError, KeyError, TypeError) as error:
                print(f"Remote retention deferred ({type(error).__name__})", file=sys.stderr)
    except (CryptError, ValueError, OSError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        if args.action == "status":
            state = {"degraded": True}
            if name and not args.cached:
                try:
                    state = update_state(root, name, degraded=True)
                except OSError:
                    pass
            print(json.dumps(status_fields(state, datetime.now(UTC))))
        if isinstance(error, CryptError):
            sys.exit(f"Encrypted backup ({error.code}): {error}")
        if isinstance(error, RecoveryError):
            sys.exit(f"Encrypted backup: {error}")
        # Local path names and subprocess arguments can include private identifiers.
        sys.exit(f"Encrypted backup failed ({type(error).__name__})")


if __name__ == "__main__":
    main()
