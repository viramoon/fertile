"""True PG17 drop/restore with a live WAL writer and precision-sensitive Parquet."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import time
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as parquet

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup", REPO / "scripts/backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_daily_weekly_retention(self):
        last = date(2026, 11, 22)
        snapshots = set()
        for i in range(42):
            for hour in ("04", "10"):
                path = self.root / ((last - timedelta(days=i)).strftime("%Y%m%d") + f"T{hour}0000.000000Z")
                path.mkdir()
                (path / "snapshot.json").write_text("{}")
                (path / "SHA256SUMS").write_text("")
                snapshots.add(path.name)
        unrelated = self.root / "keep-this-directory"
        unrelated.mkdir()
        backup.prune(SimpleNamespace(backups=self.root, days=7, weeks=4), set())
        keep = {p.name for p in backup.snapshots(self.root)}
        indices = {*range(7), 7, 14, 21}
        expected = {(last - timedelta(days=i)).strftime("%Y%m%dT100000.000000Z") for i in indices}
        self.assertEqual(keep, expected)
        self.assertTrue(unrelated.exists())
        print(f"Retention: {len(snapshots)} snapshots -> {len(keep)}, 7 days + 4 ISO weeks; unrelated dir intact")

    def test_restore(self):
        root = self.root / "box"
        data = root / "data/replay"
        state = data / "finance-state"
        catalog = data / "catalog"
        research = root / "data/research"
        socket = root / "run/postgres"
        for path in (state / "backtests", state / "collector", catalog / "data/bar/SPY.XNYS",
                     catalog / ".finance-collector", root / "config", root / "secrets", socket):
            path.mkdir(parents=True)
        cfg = root / "config/runtime.env"
        cfg.write_text("FERTILE_MARKET_SOURCE=replay\nFERTILE_POSTGRES_PORT=55461\n")
        secret = root / "secrets/web.env"
        secret.write_text("MUST_STAY_LOCAL")
        private = root / "config/optional-private.env"
        private.write_text("MUST_STAY_LOCAL")
        meta = {"source": "replay", "spec": "1-MINUTE-LAST", "id": "test-catalog-88"}
        watermark = {"SPY.XNYS": 86400000000000}
        (state / "collector/catalog.json").write_text(json.dumps(meta))
        (catalog / ".finance-collector/source.json").write_text(json.dumps(meta))
        (state / "collector/watermarks.json").write_text(json.dumps(watermark))
        work = catalog / ".finance-collector/active"
        work.mkdir()
        (work / "partial.parquet").write_text("unfinished")
        rel = Path("data/bar/SPY.XNYS/committed.parquet")
        table = pa.table({"ts_event": [watermark["SPY.XNYS"]], "close": pa.array(
            [Decimal("123.000000000123456")], type=pa.decimal128(30, 15))})
        parquet.write_table(table, catalog / rel)
        dataset = research / "TEST.XNYS/dataset.json"
        dataset.parent.mkdir(parents=True)
        dataset.write_text('{"instrument":"TEST.XNYS"}\n')
        research_rel = Path("TEST.XNYS/data/bar/published.parquet")
        (research / research_rel).parent.mkdir(parents=True)
        parquet.write_table(table, research / research_rel)
        (research / ".import-aside").mkdir()
        (research / ".import-aside/not-committed.parquet").write_text("unpublished")
        db = state / "backtests/jobs.sqlite3"
        pgdata = self.root / "pgdata"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env.update(FERTILE_ROOT=str(root), PGHOST=str(socket), PGPORT="55461", PGDATABASE="web_drill", PGUSER="drill")
        for name in ["FERTILE_DATA_ROOT", "FERTILE_RESEARCH_ROOT", "FERTILE_MARKET_SOURCE", "FERTILE_BACKUP_REMOTE", "PGSERVICE"]:
            env.pop(name, None)

        def run(*args, check=True):
            result = subprocess.run(args, env=env, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False, timeout=40)
            if check and result.returncode:
                self.fail(f"{args}: {result.stdout}\n{result.stderr}")
            return result

        def sql(statement):
            return run("psql", "-XAt", "--set", "ON_ERROR_STOP=1", "-c", statement).stdout.strip()

        run("initdb", "--auth-local=trust", "--auth-host=reject", "--no-locale", "--encoding=UTF8",
            "--no-sync", "--username=drill", "-D", pgdata)
        run("pg_ctl", "-D", pgdata, "-l", self.root / "pg.log", "-w", "start",
            "-o", f"-h '' -k '{socket}' -p 55461")
        self.addCleanup(run, "pg_ctl", "-D", pgdata, "-m", "immediate", "-w", "stop", check=False)
        active = run("psql", "-XAt", "-d", "postgres", "-c", "SHOW data_directory").stdout.strip()
        self.assertEqual(Path(active).resolve(), pgdata.resolve())
        run("createdb", "web_drill")
        sql("CREATE SCHEMA app; CREATE SCHEMA blog; "
            "CREATE TABLE app.ledger(id bigint generated always as identity, pnl numeric(30,15)); "
            "CREATE TABLE blog.entries (id int primary key, title text);"
            "INSERT INTO app.ledger(pnl) VALUES (6000000000000.123456789012345), (-0.000000000000001); "
            "INSERT INTO blog.entries VALUES (17, '备份恢复'); "
            "COMMENT ON DATABASE web_drill IS 'preserve database metadata'; "
            "REVOKE CREATE, TEMPORARY ON DATABASE web_drill FROM PUBLIC;")
        query = ("SELECT id,pnl FROM app.ledger ORDER BY id; SELECT * FROM blog.entries; "
                 "SELECT last_value,is_called FROM app.ledger_id_seq; "
                 "SELECT shobj_description(oid,'pg_database'),ARRAY(SELECT acl::text FROM unnest(datacl) acl ORDER BY acl::text) FROM pg_database WHERE datname=current_database();")
        expected = sql(query)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, idempotency_key TEXT UNIQUE)")
            conn.execute("INSERT INTO jobs VALUES(1,'queued-job')")
            conn.commit()
            conn.execute("INSERT INTO jobs VALUES(2,'uncommitted')")
            first = Path(run(REPO / "scripts/backup.sh").stdout.strip())
            first_inode = (first / "catalog" / rel).stat().st_ino
            research_inode = (first / "research" / research_rel).stat().st_ino
            conn.rollback()
            snapshot = Path(run(REPO / "scripts/backup.sh").stdout.strip())
            self.assertEqual((snapshot / "catalog" / rel).stat().st_ino, first_inode)
            self.assertEqual((snapshot / "research" / research_rel).stat().st_ino, research_inode)
        self.assertFalse((snapshot / "research/.import-aside").exists())
        self.assertFalse((snapshot / "catalog/.finance-collector/active").exists())
        self.assertFalse(any(p.name.endswith(("-wal", "-shm")) for p in snapshot.rglob("*")))
        self.assertFalse(any(b"MUST_STAY_LOCAL" in p.read_bytes() for p in snapshot.rglob("*") if p.is_file()))
        with closing(sqlite3.connect(snapshot / "finance-state/backtests/jobs.sqlite3")) as saved:
            self.assertEqual(saved.execute("SELECT * FROM jobs").fetchall(), [(1, "queued-job")])
        escape = root / "config/foo.yaml"
        escape.symlink_to(secret)
        self.assertNotEqual(run(REPO / "scripts/backup.sh", check=False).returncode, 0)
        self.assertFalse(any(root.glob("backups/.incomplete-*")))
        escape.unlink()
        dry = run(REPO / "scripts/restore.sh", snapshot, "--dry-run")
        self.assertIn("SHA256 verified", dry.stdout)
        broken = self.root / "broken"
        shutil.copytree(snapshot, broken)
        (broken / "config/runtime.env").write_text("corrupted")
        refused = run(REPO / "scripts/restore.sh", broken, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("SHA256SUMS mismatch", refused.stderr)
        self.assertEqual(sql(query), expected)
        # The replacement target may already be gone after a disk incident.
        run("dropdb", "web_drill")
        shutil.rmtree(state)
        shutil.rmtree(catalog)
        shutil.rmtree(research)
        result = run(REPO / "scripts/restore.sh", snapshot)
        self.assertIn("Before-restore snapshot:", result.stdout)
        self.assertEqual(sql(query), expected)
        self.assertEqual(parquet.read_table(catalog / rel), table)
        self.assertEqual(parquet.read_table(research / research_rel), table)
        self.assertEqual(dataset.read_text(), '{"instrument":"TEST.XNYS"}\n')
        self.assertNotEqual((catalog / rel).stat().st_ino, (snapshot / "catalog" / rel).stat().st_ino)
        self.assertEqual(json.loads((state / "collector/watermarks.json").read_text()), watermark)
        self.assertEqual(json.loads((catalog / ".finance-collector/source.json").read_text()), meta)
        self.assertEqual(json.loads((state / "collector/catalog.json").read_text()), meta)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM jobs").fetchall(), [(1, "queued-job")])
        sql("CREATE TABLE app.post_restore_only (x int);")
        # Interrupt only this drill's restore process group AFTER the previous
        # database was renamed. The next invocation must recover it first.
        guard_dir = self.root / "commands"
        guard_dir.mkdir()
        gate = self.root / "reached-create"
        shim = guard_dir / "pg_restore"
        shim.write_text("#!/bin/sh\nif test \"$1\" = --create; then touch \"$DRILL_GATE\"; sleep 120; fi\n"
                        "exec \"$REAL_RESTORE\" \"$@\"\n")
        shim.chmod(0o700)
        interrupted_env = dict(env, PATH=str(guard_dir) + ":" + env["PATH"], DRILL_GATE=str(gate),
                               REAL_RESTORE=shutil.which("pg_restore"))
        with subprocess.Popen([REPO / "scripts/restore.sh", snapshot], start_new_session=True,
                              env=interrupted_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as restore:
            try:
                deadline = time.monotonic() + 20
                while not gate.exists():
                    self.assertIsNone(restore.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.05)
                self.assertTrue((root / "run/restore-journal.json").exists())
            finally:
                if restore.poll() is None:
                    os.killpg(restore.pid, signal.SIGKILL)
                restore.wait()
            self.assertEqual(restore.returncode, -signal.SIGKILL)
        result = run(REPO / "scripts/restore.sh", snapshot)
        self.assertIn("Interrupted restore reconciled.", result.stdout)
        self.assertFalse((root / "run/restore-journal.json").exists())
        self.assertEqual(sql("SELECT to_regclass('app.post_restore_only') IS NULL"), "t")
        self.assertEqual(sql(query), expected)
        prior = Path(next(s.split(": ", 1)[1] for s in result.stdout.splitlines()
                          if s.startswith("Before-restore snapshot:")))
        self.assertIn("post_restore_only", run("pg_restore", "-l", prior / "postgres.dump").stdout)
        self.assertEqual(secret.read_text(), "MUST_STAY_LOCAL")
        self.assertEqual(private.read_text(), "MUST_STAY_LOCAL")
        legacy = self.root / "legacy"
        shutil.copytree(snapshot, legacy)
        shutil.rmtree(legacy / "research")
        manifest = legacy / "snapshot.json"
        meta_without_research = json.loads(manifest.read_text())
        del meta_without_research["trees"]["research"]
        manifest.write_text(json.dumps(meta_without_research))
        (legacy / "SHA256SUMS").write_text("".join(
            f"{backup.digest(legacy / path)}  {path}\n" for path in sorted(backup.members(legacy))))
        run(REPO / "scripts/restore.sh", legacy)
        self.assertEqual(parquet.read_table(research / research_rel), table)
        self.assertTrue(dataset.exists())
        print(run("pg_config", "--version").stdout.strip())
        print(result.stdout.strip())
        print(expected)
        print("Checksum mismatch refused before writes; restore twice, secrets unchanged; "
              "SQLite WAL, market and research Parquet, source manifest and watermarks match. "
              "Restoring a legacy snapshot preserved the research catalog.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
