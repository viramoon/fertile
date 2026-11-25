"""Snapshot and restore the cloud-box database, published catalog and application state."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

STAMP = "%Y%m%dT%H%M%S.%fZ"
BASE_TREES = {"finance-state", "config", "catalog"}
ALLOWED_TREES = {*BASE_TREES, "research"}
SIDECARS = ("-wal", "-shm", "-journal")


def utc_stamp():
    return datetime.now(timezone.utc).strftime(STAMP)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def valid_database(name):
    return (isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name)
            and name not in {"postgres", "template0", "template1"})


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(root):
    for parent, _, names in os.walk(root, topdown=False):
        for name in names:
            with (Path(parent) / name).open("rb") as handle:
                os.fsync(handle.fileno())
        sync_dir(parent)


def atomic_json(path, content):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        json.dump(content, f, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)
    sync_dir(path.parent)


def runtime_settings(path):
    if not path.exists():
        return {}
    require(not path.is_symlink() and path.is_file(), "runtime.env must be a regular file")
    settings = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, sep, value = line.partition("=")
            require(bool(sep), "invalid runtime.env entry")
            if value.startswith(('"', "'")) and value.endswith(value[0]):
                value = value[1:-1]
            settings[key] = value
    return settings


class Box:
    def __init__(self, interrupted=None, *, restoring=False):
        self.root = Path(os.getenv("FERTILE_ROOT", "/home/box/fertile")).resolve(strict=True)
        self.config = self.root / "config"
        overrides = runtime_settings(self.config / "backup.conf")
        allowed = {"FERTILE_BACKUP_CATALOG", "FERTILE_BACKUP_DAYS", "FERTILE_BACKUP_WEEKS",
                   "FERTILE_BACKUP_REMOTE"}
        require(overrides.keys() <= allowed, "unknown config/backup.conf key")
        settings = {**runtime_settings(self.config / "runtime.env"), **overrides, **os.environ}
        if interrupted:
            settings.update(interrupted["location"])
        self.fds = []
        self.source = settings.get("FERTILE_MARKET_SOURCE", "ib")
        require(self.source in {"ib", "replay"}, "invalid FERTILE_MARKET_SOURCE")
        default = self.root / ("data/replay" if self.source == "replay" else "data")
        self.data = Path(settings.get("FERTILE_DATA_ROOT", default))
        require(self.data.is_absolute() and (restoring or self.data.is_dir()), "active data root is missing")
        self.data = self.data.resolve()
        self.catalog = Path(settings.get("FERTILE_BACKUP_CATALOG", self.data / "catalog"))
        research_base = self.root / "data/research"
        self.research = Path(settings.get("FERTILE_RESEARCH_ROOT", research_base))
        require(self.research.is_relative_to(research_base) or self.research == self.data / "research",
                "research catalog must be within fertile data/research or at FERTILE_DATA_ROOT/research")
        self.backups = self.root / "backups"
        self.run = self.root / "run"
        self.trees = {"finance-state": self.data / "finance-state", "catalog": self.catalog,
                      "config": self.config, "research": self.research}
        roots = [self.backups, self.root / "secrets", self.run, *self.trees.values()]
        for path in roots:
            require(path.is_absolute() and path == path.resolve(), f"unsafe directory: {path}")
            require(all(not q.is_relative_to(path) for q in roots if q != path)
                    and roots.count(path) == 1, "overlapping application and backup directories")
        self.db = settings.get("PGDATABASE", settings.get("FERTILE_WEB_DATABASE", "web"))
        if not restoring or interrupted is not None:
            require(valid_database(self.db), "invalid application database")
        self.pg = dict(os.environ, LC_ALL="C", PGHOST=settings.get("PGHOST", str(self.run / "postgres")),
                       PGPORT=settings.get("PGPORT", settings.get("FERTILE_POSTGRES_PORT", "5432")),
                       PGCONNECT_TIMEOUT=settings.get("PGCONNECT_TIMEOUT", "5"))
        self.days = int(settings.get("FERTILE_BACKUP_DAYS", 7))
        self.weeks = int(settings.get("FERTILE_BACKUP_WEEKS", 4))
        require(self.days >= 1 and self.weeks >= 0, "retention requires days>=1 and weeks>=0")
        self.remote = settings.get("FERTILE_BACKUP_REMOTE", "")

    def command(self, *args, sql=None):
        return subprocess.run(args, input=sql, env=self.pg, check=True,
                              stdout=subprocess.PIPE, text=True, pass_fds=self.fds).stdout.strip()

    def sql(self, sql, database="postgres"):
        return self.command("psql", "-XqAt", "--set", "ON_ERROR_STOP=1",
                            "--dbname", database, sql=sql)

    def exists(self, db):
        return self.sql(f"SELECT EXISTS(SELECT FROM pg_database WHERE datname='{db}')") == "t"

    def select_targets(self, data, database, research):
        require(valid_database(database), "invalid --target-database")
        require(data.is_absolute() and data == data.resolve() and data.parent.is_dir()
                and (not data.exists() or data.is_dir()),
                "--target-data-root must be an absolute concrete directory with an existing parent")
        require(not self.root.is_relative_to(data), "restore target overlaps FERTILE_ROOT")
        protected = [(self.root / name).resolve() for name in ("config", "secrets", "run", "backups", "releases",
                                                              "current", "tools", "logs", "data/postgres", "data/ibgateway")]
        require(all(not data.is_relative_to(path) for path in protected), "restore target is a protected directory")
        active = (self.data, self.catalog, self.research)
        if data != self.data:
            require(all(not overlaps(data, path) for path in [*protected, *active]),
                    "restore target overlaps an active or protected directory")
        if research is not None:
            require(research.is_relative_to(self.root / "data/research") and research.parent.is_dir(),
                    "--target-research-root must be within fertile data/research with an existing parent")
            require(not overlaps(research, self.research) or data == self.data and research == self.research,
                    "restore research target overlaps the active research catalog")
            if data != self.data:
                require(all(not overlaps(research, path) for path in active),
                        "restore research target overlaps an active directory")
        self.data = data
        self.catalog = data / "catalog"
        self.research = research if research is not None else data / "research"
        self.db = database
        self.trees = {"finance-state": data / "finance-state", "catalog": self.catalog, "research": self.research}
        for key, path in self.trees.items():
            require(path == path.resolve() and (not path.exists() or path.is_dir())
                    and all(not overlaps(path, other) for other in protected)
                    and all(not overlaps(path, other) for name, other in self.trees.items() if name != key),
                    f"unsafe restore destination: {path}")


def overlaps(path, other):
    return path.is_relative_to(other) or other.is_relative_to(path)


@contextmanager
def locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield handle.fileno()


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def secret_name(name):
    lower = name.lower()
    return (lower.startswith((".env", ".pgpass")) or lower in {"secrets", ".cloudflared"}
            or any(word in lower for word in ("credential", "private_key", "private-key", "password", "secret"))
            or lower.endswith((".env", ".pem", ".key", ".p12", ".pfx")) and lower != "runtime.env")


def payload_files(tree, kind):
    if tree is None or not tree.exists():
        return
    require(tree.is_dir() and not tree.is_symlink(), f"invalid {kind} root")
    for folder, dirs, names in os.walk(tree):
        base = Path(folder)
        dirs.sort()
        for name in dirs.copy():
            path = base / name
            if (secret_name(name) or name.startswith(("run-", ".")) and kind == "finance-state"
                    or path == tree / ".finance-collector/active" and kind == "catalog"
                    or kind == "research" and name.startswith(".")):
                dirs.remove(name)
            else:
                require(not path.is_symlink(), f"symlink found in {kind}")
        for name in sorted(names):
            path = base / name
            rel = path.relative_to(tree)
            if name.endswith(".lock") or secret_name(name) or name.startswith(".") and kind == "finance-state":
                continue
            if kind == "catalog" and path.suffix != ".parquet" and rel.as_posix() != ".finance-collector/source.json":
                continue
            if kind == "research" and path.suffix != ".parquet" and not (
                    len(rel.parts) == 2 and name == "dataset.json"):
                continue
            if kind == "finance-state" and name.endswith(SIDECARS):
                continue
            require(path.is_file() and not path.is_symlink(), f"unexpected file in {kind}: {rel}")
            yield rel


def copy_sqlite(src, dst):
    with closing(sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)) as db, closing(sqlite3.connect(dst)) as copy:
        deadline = time.monotonic() + 300
        least_pages = float("inf")

        def progress(_status, remaining, _total):
            nonlocal least_pages, deadline
            if remaining < least_pages:
                least_pages = remaining
                deadline = time.monotonic() + 300
            require(time.monotonic() < deadline, "SQLite backup has stalled for five minutes")

        db.backup(copy, pages=1024, progress=progress, sleep=.1)
        require(copy.execute("PRAGMA quick_check").fetchall() == [("ok",)], "SQLite check failed")
        copy.execute("PRAGMA journal_mode=DELETE").fetchone()


def copy_tree_payload(src, dst, kind, previous):
    dst.mkdir()
    for rel in payload_files(src, kind):
        source, target = src / rel, dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "finance-state" and source.suffix in (".db", ".sqlite", ".sqlite3"):
            copy_sqlite(source, target)
        elif kind in {"catalog", "research"} and previous and rel.suffix == ".parquet":
            old = previous / kind / rel
            if old.is_file() and not old.is_symlink() and digest(source) == digest(old):
                os.link(old, target)
            else:
                shutil.copyfile(source, target)
        else:
            shutil.copyfile(source, target)


def check_provenance(snapshot):
    collector = snapshot / "finance-state/collector"
    binding = collector / "catalog.json"
    marker = snapshot / "catalog/.finance-collector/source.json"
    if marker.exists() or binding.exists() or (collector / "watermarks.json").exists():
        require(marker.is_file() and binding.is_file() and
                json.loads(marker.read_bytes()) == json.loads(binding.read_bytes()),
                "collector and catalog have different provenance")
        require(isinstance(json.loads((collector / "watermarks.json").read_bytes()), dict),
                "collector checkpoint is missing or invalid")


def members(snapshot):
    for parent, dirs, names in os.walk(snapshot):
        base = Path(parent)
        for name in dirs + names:
            require(not (base / name).is_symlink(), "snapshot contains symlink")
        for name in names:
            path = base / name
            rel = path.relative_to(snapshot)
            require(path.is_file() and not any(ord(c) < 32 or c == "\\" for c in str(rel)),
                    "unsupported snapshot member")
            if rel.as_posix() != "SHA256SUMS":
                require(rel.parts[0] in ALLOWED_TREES or str(rel) in {"postgres.dump", "snapshot.json"},
                        "unexpected snapshot member")
                yield rel


def verify(snapshot):
    require(snapshot.is_dir() and not snapshot.is_symlink(), "snapshot directory required")
    manifest = snapshot / "SHA256SUMS"
    require(manifest.is_file() and not manifest.is_symlink(), "missing regular SHA256SUMS")
    expected = {}
    for line in manifest.read_text().splitlines():
        hash_, sep, name = line.partition("  ")
        require(bool(sep) and re.fullmatch("[0-9a-f]{64}", hash_) and name not in expected, "invalid SHA256SUMS")
        expected[name] = hash_
    actual = {str(path): digest(snapshot / path) for path in members(snapshot)}
    require(actual == expected, "SHA256SUMS mismatch")
    meta = json.loads((snapshot / "snapshot.json").read_bytes())
    declared = set(meta["trees"])
    require(meta["format"] == 1 and declared in (BASE_TREES, ALLOWED_TREES) and
            all(type(present) is bool for present in meta["trees"].values()), "unknown snapshot format")
    require(all((snapshot / key).is_dir() for key in declared)
            and (not (snapshot / "research").exists() or "research" in declared), "snapshot tree mismatch")
    require(type(meta["has_database"]) is bool and meta["has_database"] == (snapshot / "postgres.dump").is_file(),
            "snapshot database dump does not match manifest")
    check_provenance(snapshot)
    return meta


def snapshots(root):
    for path in sorted(root.iterdir(), reverse=True):
        if (not path.is_symlink() and path.is_dir() and re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z", path.name)
                and (path / "snapshot.json").is_file() and (path / "SHA256SUMS").is_file()):
            yield path


def prune(box, protected):
    days, weeks = set(), set()
    for path in snapshots(box.backups):
        date = datetime.strptime(path.name, STAMP).date()
        week = date.isocalendar()[:2]
        daily = date not in days and len(days) < box.days
        weekly = week not in weeks and len(weeks) < box.weeks
        days.add(date)
        weeks.add(week)
        if not (daily or weekly or path in protected):
            shutil.rmtree(path)
    sync_dir(box.backups)


def backup(box, recovery=False):
    box.backups.mkdir(mode=0o700, exist_ok=True)
    previous = next(snapshots(box.backups), None)
    stage = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=box.backups))
    try:
        has_db = box.exists(box.db)
        require(has_db or recovery, "application DB missing")
        if has_db:
            box.command("pg_dump", "--format=custom", "--create", "--dbname", box.db, "--file", stage / "postgres.dump")
        trees = {name: name in box.trees and box.trees[name].is_dir() for name in ALLOWED_TREES}
        # Copy the atomic checkpoint first. Immutable catalog files enumerated
        # later can only be newer; the collector recomputes its watermark at open.
        for name in ("finance-state", "catalog", "config", "research"):
            copy_tree_payload(box.trees.get(name), stage / name, name, previous)
        check_provenance(stage)
        stamp = utc_stamp()
        atomic_json(stage / "snapshot.json", dict(format=1, stamp=stamp, source=box.source, database=box.db,
                                                  has_database=has_db, trees=trees,
                                                  purpose="before-restore" if recovery else "scheduled-or-manual"))
        files = sorted(members(stage))
        with (stage / "SHA256SUMS").open("x") as f:
            for rel in files:
                f.write(f"{digest(stage / rel)}  {rel}\n")
        sync_tree(stage)
        result = box.backups / stamp
        stage.rename(result)
        sync_dir(box.backups)
        if not recovery:
            if box.remote:
                require(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*:[^\r\n]*", box.remote), "use a named rclone remote")
                box.command("rclone", "copy", "--", str(result), box.remote.rstrip("/") + "/" + stamp)
            prune(box, {result})
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def remove_tree(path):
    if path.exists():
        require(path.is_dir() and not path.is_symlink(), "unexpected path during restore")
        shutil.rmtree(path)
        sync_dir(path.parent)


def quiescent(box):
    pid = box.run / "supervisord.pid"
    if pid.is_file():
        try:
            os.kill(int(pid.read_text().strip()), 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("run fertile down, then start PostgreSQL alone for restore")
    active = box.sql(f"SELECT count(*) FROM pg_stat_activity WHERE datname='{box.db}' AND backend_type='client backend'")
    require(active == "0", "stop database clients before restore")
    # Backup supports concurrent writers; swapping the directories during restore does not.
    for path in [box.catalog / ".finance-collector/writer.lock",
                 box.data / "finance-state/collector/writer.lock", box.data / "finance-state/backtests/worker.lock"]:
        if path.is_file():
            with path.open("rb") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as e:
                    raise ValueError("stop the collector and backtest worker before restore") from e


def rollback_or_finish(box, journal):
    info = json.loads(journal.read_bytes())
    require(info["db"] == box.db, "resume restore using its original PGDATABASE")
    committed = info["phase"] == "committed"
    old_db = info["old_db"]
    require(re.fullmatch("fertile_old_[0-9a-f]{32}", old_db), "invalid recovery marker")
    if info["restore_db"]:
        if committed:
            if box.exists(old_db):
                box.command("dropdb", "--maintenance-db=postgres", old_db)
        elif box.exists(old_db):
            if box.exists(box.db):
                box.command("dropdb", "--maintenance-db=postgres", box.db)
            box.sql(f'ALTER DATABASE "{old_db}" RENAME TO "{box.db}"')
        elif not info["had_db"] and box.exists(box.db):
            box.command("dropdb", "--maintenance-db=postgres", box.db)
    for row in info["trees"]:
        path = Path(row["path"])
        # Use the original local paths even if an old config/runtime.env was
        # temporarily swapped in before the interruption.
        expected = ".fertile-restore-" + old_db.removeprefix("fertile_old_") + "-" + path.name
        stage = path.parent / (expected + ".new")
        retired = path.parent / (expected + ".old")
        if committed:
            remove_tree(retired)
        elif retired.exists():
            remove_tree(path)
            retired.rename(path)
            sync_dir(path.parent)
        elif not row["had"] and row["install"] and not stage.exists():
            remove_tree(path)
        remove_tree(stage)
    if not committed and info.get("created_data"):
        data = Path(info["created_data"])
        if data.is_dir():
            data.rmdir()
            sync_dir(data.parent)
    journal.unlink()
    sync_dir(journal.parent)


def verify_restore(box, snapshot, targets):
    meta = verify(snapshot)
    require(valid_database(meta["database"]), "invalid database in snapshot manifest")
    require(meta["source"] == box.source, "snapshot source differs from selected target")
    if meta["has_database"]:
        toc = box.command("pg_restore", "--list", snapshot / "postgres.dump")
        archive_db = re.search(r"^;[ \t]+dbname:[ \t]+(\S+)[ \t]*$", toc, re.MULTILINE)
        require(re.search(r"^;[ \t]+Format: CUSTOM[ \t]*$", toc, re.MULTILINE), "expected a custom PostgreSQL archive")
        require(archive_db and archive_db[1] == meta["database"], "archive database differs from snapshot manifest")
    if targets is not None:
        box.select_targets(*targets)
        require(all(not overlaps(snapshot.resolve(), path) for path in (box.data, *box.trees.values())),
                "snapshot overlaps the restore destination")
    target = f", target_database={box.db}, target_data_root={box.data}" if targets is not None else ""
    print(f"SHA256 verified: {snapshot.name}, database={meta['database']}, source={box.source}{target}, "
          f"has_database={meta['has_database']}", flush=True)
    return meta


def database_empty(box):
    # Use pg_dump's object selection to distinguish application objects from the
    # built-in schemas, including empty application schemas and database extensions.
    if box.sql("SELECT EXISTS (SELECT FROM pg_largeobject_metadata)", box.db) != "f":
        return False
    with tempfile.TemporaryDirectory(prefix="fertile-empty-") as directory:
        archive = Path(directory) / "schema.dump"
        box.command("pg_dump", "--schema-only", "--no-comments", "--no-privileges", "--format=custom",
                    "--dbname", box.db, "--file", archive)
        toc = box.command("pg_restore", "--list", archive)
    standard = re.compile(r"\d+; (?:2615 2200 SCHEMA - public \S+|0 0 (?:ENCODING|STDSTRINGS|SEARCHPATH) - .*)")
    return not any(re.match(r"^\d+;", line) and not standard.fullmatch(line) for line in toc.splitlines())


def restore(box, snapshot, meta, overwrite, targets):
    with locked(box.run / "control.lock") as control_fd:
        box.fds.append(control_fd)
        quiescent(box)
        had_db = box.exists(box.db)
        if not overwrite:
            for path in {box.data, *box.trees.values()}:
                require(not path.exists() or not any(path.iterdir()), f"non-empty restore directory: {path}; use --overwrite")
            require(not had_db or database_empty(box), f"non-empty target database: {box.db}; use --overwrite")
        journal = box.run / "restore-journal.json"
        before = backup(box, recovery=True)
        print(f"Before-restore snapshot: {before}", flush=True)
        token = uuid.uuid4().hex
        old_db = "fertile_old_" + token
        plan = dict(phase="preparing", db=box.db, old_db=old_db, had_db=had_db,
                    restore_db=meta["has_database"], trees=[],
                    targets=[str(value) if value is not None else None for value in targets],
                    created_data=str(box.data) if not box.data.exists() else None,
                    location=dict(PGHOST=box.pg["PGHOST"], PGPORT=box.pg["PGPORT"], PGDATABASE=box.db,
                                  FERTILE_DATA_ROOT=str(box.data), FERTILE_MARKET_SOURCE=box.source,
                                  FERTILE_BACKUP_CATALOG=str(box.catalog),
                                  FERTILE_RESEARCH_ROOT=str(box.research)))
        for key, path in box.trees.items():
            if key not in meta["trees"]:  # Snapshots taken before research must leave its catalog intact.
                continue
            plan["trees"].append(dict(key=key, path=str(path), had=path.is_dir(), install=meta["trees"][key]))
        atomic_json(journal, plan)
        try:
            if plan["created_data"]:
                box.data.mkdir()
                sync_dir(box.data.parent)
            for row in plan["trees"]:
                if row["install"]:
                    path = Path(row["path"])
                    stage = path.parent / (".fertile-restore-" + token + "-" + path.name + ".new")
                    shutil.copytree(snapshot / row["key"], stage)
                    sync_tree(stage)
            if meta["has_database"]:
                if plan["had_db"]:
                    box.sql(f'ALTER DATABASE "{box.db}" RENAME TO "{old_db}"')
                if meta["database"] == box.db:
                    # Keep database-level metadata when the archive name is usable.
                    box.command("pg_restore", "--create", "--exit-on-error", "--dbname=postgres", snapshot / "postgres.dump")
                    require(box.exists(box.db), "archive did not recreate the expected database")
                else:
                    box.command("createdb", "--maintenance-db=postgres", "--template=template0", box.db)
                    box.command("pg_restore", "--exit-on-error", "--dbname", box.db, snapshot / "postgres.dump")
            for row in plan["trees"]:
                path = Path(row["path"])
                stem = ".fertile-restore-" + token + "-" + path.name
                if row["had"]:
                    path.rename(path.parent / (stem + ".old"))
                if row["install"]:
                    (path.parent / (stem + ".new")).rename(path)
                sync_dir(path.parent)
            plan["phase"] = "committed"
            atomic_json(journal, plan)
        finally:
            rollback_or_finish(box, journal)
        print(f"Restored: {snapshot.name}. Pre-restore state: {before}")


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("backup")
    sub = actions.add_parser("restore", prog="scripts/restore.sh")
    sub.add_argument("snapshot", type=Path)
    sub.add_argument("--target-data-root", type=Path, help="absolute destination for finance-state, catalog and research")
    sub.add_argument("--target-database", help="destination PostgreSQL database name on the configured server")
    sub.add_argument("--target-research-root", type=Path, help="explicit research destination under FERTILE_ROOT/data/research")
    sub.add_argument("--overwrite", action="store_true", help="allow replacing payload in non-empty destinations")
    sub.add_argument("--dry-run", action="store_true", help="verify SHA256, format and explicit target paths without writes")
    args = parser.parse_args()
    targets = None
    if args.action == "restore":
        if args.target_data_root is not None and args.target_database is not None:
            targets = (args.target_data_root, args.target_database, args.target_research_root)
        elif not args.dry_run or any(value is not None for value in
                                     (args.target_data_root, args.target_database, args.target_research_root)):
            sub.error("specify both --target-data-root and --target-database")
    os.umask(0o077)
    root = Path(os.getenv("FERTILE_ROOT", "/home/box/fertile")).resolve(strict=True)
    journal = root / "run/restore-journal.json"
    if args.action == "restore" and args.dry_run:
        verify_restore(Box(restoring=True), args.snapshot, targets)
        return
    with locked(root / "run/backup.lock") as backup_fd:
        if journal.exists():
            require(args.action == "restore", "interrupted restore must be reconciled first")
            saved = json.loads(journal.read_bytes())
            expected = saved.get("targets", [saved["location"]["FERTILE_DATA_ROOT"], saved["db"], None])
            require([str(path) if path is not None else None for path in targets] == expected,
                    "interrupted restore must be reconciled using its original explicit targets")
            interrupted = Box(saved, restoring=True)
            with locked(interrupted.run / "control.lock") as control_fd:
                interrupted.fds = [backup_fd, control_fd]
                quiescent(interrupted)
                rollback_or_finish(interrupted, journal)
                print("Interrupted restore reconciled.", flush=True)
        box = Box(restoring=args.action == "restore")
        box.fds = [backup_fd]
        if args.action == "backup":
            print(backup(box))
        else:
            meta = verify_restore(box, args.snapshot, targets)
            restore(box, args.snapshot, meta, args.overwrite, targets)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.CalledProcessError) as error:
        sys.exit(f"Backup/restore: {error}")
