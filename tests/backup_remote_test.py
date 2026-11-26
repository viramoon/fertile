"""Recovery points with generated SQLite data, synthetic dumps and a local crypt remote."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup
import backup_remote as remote_backup
from backup_crypt import CryptError, CryptRemote

RCLONE = shutil.which(os.environ.get("FERTILE_TEST_RCLONE", "rclone"))
TEST_POSTGRES = os.environ.get("FERTILE_TEST_POSTGRES") == "1"
UTC = timezone.utc


@unittest.skipUnless(RCLONE, "Install rclone v1.76.0 or set FERTILE_TEST_RCLONE")
class EncryptedRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fertile-backup-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.box = self.root / "box"
        self.store = self.root / "encrypted"
        self.store.mkdir()
        self.config = self.box / "secrets/backup-rclone.conf"
        self.config.parent.mkdir(mode=0o700, parents=True)
        # Random test keys are generated anew and never printed or reused.
        def key():
            return subprocess.check_output([RCLONE, "obscure", "-"], input=os.urandom(24).hex(),
                                           text=True, env={"PATH": os.defpath}).strip()
        self.config.write_text(f"[local_fixture]\ntype = local\n[private]\ntype = crypt\n"
                               f"remote = local_fixture:{self.store}\npassword = {key()}\npassword2 = {key()}\n")
        self.config.chmod(0o600)
        self.data = self.box / "data/replay"
        self.state = self.data / "finance-state"
        self.state.mkdir(parents=True)
        self.catalog = self.data / "catalog"
        self.catalog.mkdir()
        self.marker = "fixture-private-བྱ་"
        self.old = self.catalog / (self.marker + "-deleted.parquet")
        self.old.write_text(self.marker + " deterministic old bytes")
        self.stable = self.catalog / "unchanged.parquet"
        self.stable.write_bytes(b"synthetic unchanged parquet placeholder")
        self.update_queue(1)
        settings = self.box / "config"
        settings.mkdir()
        (settings / "backup.conf").write_text(f"FERTILE_BACKUP_REMOTE=private:fixture\nFERTILE_BACKUP_RCLONE={RCLONE}\n")
        self.release = {"fertile": "1" * 40, "components": {"finance": "2" * 40, "web": "3" * 40, "frontend": "4" * 40}}
        release_dir = self.box / "releases" / self.release["fertile"]
        release_dir.mkdir(parents=True)
        (release_dir / "manifest.json").write_text(json.dumps({"format": 1, **self.release}))
        (self.box / "current").symlink_to(release_dir.relative_to(self.box))
        excluded = settings / "credentials"
        excluded.mkdir()
        (excluded / "nested.json").write_text("never back up this synthetic secret")
        env = patch.dict(os.environ, {"PATH": os.environ.get("PATH", os.defpath),
                                      "FERTILE_ROOT": str(self.box), "FERTILE_MARKET_SOURCE": "replay",
                                      "FERTILE_DATA_ROOT": str(self.data)}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def update_queue(self, revision):
        with closing(sqlite3.connect(self.state / "queue.sqlite3")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (revision INTEGER)")
            db.execute("INSERT INTO jobs VALUES (?)", (revision,))

    def invoke(self, *args, cut=None, real_pg=False):
        def pg_command(_box, *command, **_kwargs):
            if command[0] == "psql":
                return "t"
            if command[0] == "pg_dump":
                Path(command[command.index("--file") + 1]).write_text(self.marker + " synthetic dump bytes")
                return ""
            raise AssertionError(command[0])
        output, errors = io.StringIO(), io.StringIO()
        status = None
        pg = nullcontext() if real_pg else patch.object(backup.Box, "command", pg_command)
        with (patch.object(sys, "argv", ["backup_remote.py", *map(str, args)]),
              patch.object(backup, "utc_stamp", side_effect=(lambda: cut) if cut else backup.utc_stamp),
              pg, redirect_stdout(output), redirect_stderr(errors)):
            try:
                remote_backup.main()
            except SystemExit as error:
                status = error.code
        return status, output.getvalue().splitlines(), errors.getvalue()

    def success(self, *args, cut=None, real_pg=False):
        status, lines, errors = self.invoke(*args, cut=cut, real_pg=real_pg)
        self.assertIsNone(status, (status, errors))
        return json.loads(lines[0])

    def transport(self):
        return CryptRemote(RCLONE, self.config, "private:fixture")

    def read_inventory(self):
        with tempfile.TemporaryDirectory(dir=self.root) as work:
            return remote_backup.inventory(self.transport(), Path(work))

    def encrypted_files(self):
        return {path for path in self.store.rglob("*") if path.is_file()}

    def corrupt_ciphertext(self, new_files, plaintext_size):
        size = remote_backup.ciphertext_size(plaintext_size)
        candidates = [path for path in new_files if path.stat().st_size == size]
        self.assertEqual(len(candidates), 1, "fixture must identify exactly one encrypted object")
        blob, = candidates
        data = bytearray(blob.read_bytes())
        data[-1] ^= 1
        blob.write_bytes(data)
        self.assertEqual(blob.stat().st_size, size)

    def test_standalone_full_and_hourly_restore_exact_point(self):
        original = self.success("full")["point"]
        self.old.unlink()
        replacement = self.catalog / "new.parquet"
        replacement.write_bytes(b"generated hourly revision 2")
        self.update_queue(2)
        hour = self.success("hourly")["point"]
        replacement.write_bytes(b"generated hourly revision 3")
        latest = self.success("hourly")["point"]
        found = self.read_inventory()
        self.assertEqual([p.id for p in found.chain(latest)], [original, hour, latest])
        self.assertNotIn("catalog/unchanged.parquet", found.points[hour].files)
        self.assertIn("catalog/" + self.old.name, found.points[hour].deleted)
        self.assertFalse(found.points[original].parent)
        for point, expected, revisions in ((original, None, [1]), (hour, b"generated hourly revision 2", [1, 2])):
            with self.subTest(point=point):
                target = self.root / point
                self.success("restore", point, "--snapshot-to", target)
                meta = backup.verify(target)
                self.assertEqual(meta["stamp"], point)
                self.assertEqual(meta["release"], self.release)
                self.assertEqual((target / "catalog/unchanged.parquet").read_bytes(), self.stable.read_bytes())
                new = target / "catalog/new.parquet"
                self.assertEqual(new.read_bytes() if new.exists() else None, expected)
                self.assertEqual((target / "catalog" / self.old.name).exists(), point == original)
                self.assertFalse((target / "config/credentials").exists())
                with closing(sqlite3.connect(target / "finance-state/queue.sqlite3")) as db:
                    self.assertEqual([row[0] for row in db.execute("SELECT revision FROM jobs")], revisions)

    @unittest.skipUnless(TEST_POSTGRES, "Set FERTILE_TEST_POSTGRES=1 for the isolated PG17 drill")
    def test_postgres_and_sqlite_restore_into_empty_targets(self):
        pg_data = self.root / "pg"
        pg_socket = self.root / "socket"
        pg_socket.mkdir()

        def command(*args):
            return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)

        command("initdb", "-D", pg_data, "--no-locale", "--auth=trust")
        log = self.root / "postgres.log"
        try:
            command("pg_ctl", "-D", pg_data, "-l", log, "-w", "start",
                    "-o", f"-c listen_addresses='' -c unix_socket_directories='{pg_socket}'")
        except subprocess.CalledProcessError:
            self.fail(log.read_text())
        self.addCleanup(command, "pg_ctl", "-D", pg_data, "-w", "stop", "-m", "immediate")
        os.environ.update(PGHOST=str(pg_socket), PGDATABASE="web_fixture")
        command("createdb", "--maintenance-db=postgres", "web_fixture")
        command("psql", "-Xqc", "CREATE TABLE fixture (amount numeric(24,8)); "
                "INSERT INTO fixture VALUES (1234567890123456.00000001)")
        full = self.success("full", real_pg=True)["point"]
        command("psql", "-Xqc", "INSERT INTO fixture VALUES (0.00000002)")
        self.update_queue(2)
        hour = self.success("hourly", real_pg=True)["point"]
        for point, name, amounts, count in (
                (full, "daily_fixture", ["1234567890123456.00000001"], 1),
                (hour, "hourly_fixture", ["0.00000002", "1234567890123456.00000001"], 2)):
            with self.subTest(point=point):
                data = self.root / name
                self.assertFalse(data.exists())
                status, output, _ = self.invoke("restore", point, "--target-data-root", data,
                                                "--target-database", name, real_pg=True)
                self.assertIsNone(status)
                self.assertTrue(any(line.startswith("Restored:") for line in output))
                self.assertEqual(command("psql", "-XAt", "--dbname", name, "-c",
                                         "SELECT amount FROM fixture ORDER BY amount").splitlines(), amounts)
                with closing(sqlite3.connect(data / "finance-state/queue.sqlite3")) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone(), (count,))
                self.assertEqual(json.loads(remote_backup.receipt_file(self.box).read_text())["point"]["id"], point)

    def test_remote_contains_ciphertext_names_and_bytes(self):
        point = self.success("full")["point"]
        encrypted = list(self.store.rglob("*"))
        self.assertTrue(any(path.is_file() for path in encrypted))
        for path in encrypted:
            for plaintext in (self.marker, point, "manifest", "commits", "objects", "snapshot.json"):
                self.assertNotIn(plaintext, path.name)
                if path.is_file():
                    self.assertNotIn(plaintext.encode(), path.read_bytes())
        self.assertIn(point + ".json", self.transport().list_files("commits"))

    def test_upload_failure_never_advances_remote_commit_or_cached_point(self):
        full = self.success("full")["point"]
        def fail_after_upload(transport, source, destination):
            original(transport, source, destination)
            raise CryptError("remote_error", "synthetic transfer interrupted")
        original = CryptRemote.upload_tree
        self.update_queue(2)
        with patch.object(CryptRemote, "upload_tree", fail_after_upload):
            status, _, _ = self.invoke("hourly")
        self.assertIn("remote_error", status)
        self.assertEqual(self.success("status", "--cached")["last_recoverable_point"], full)
        self.assertEqual(self.success("status")["last_recoverable_point"], full)
        self.assertEqual(set(self.transport().list_files("commits")), {full + ".json"})
        self.assertEqual(len(self.read_inventory().objects), 2)
        self.assertNotEqual(self.success("hourly")["point"], full)

    def test_commit_failure_and_response_lost_after_commit(self):
        full = self.success("full")["point"]
        with patch.object(CryptRemote, "upload", side_effect=CryptError("remote_error", "synthetic commit failed")):
            self.assertIn("remote_error", self.invoke("hourly")[0])
        self.assertEqual(self.success("status")["last_recoverable_point"], full)
        original = CryptRemote.upload
        def write_then_lose_response(transport, source, destination):
            original(transport, source, destination)
            raise CryptError("remote_error", "synthetic lost response")
        with patch.object(CryptRemote, "upload", write_then_lose_response):
            hour = self.success("hourly")["point"]
        self.assertEqual(self.success("status")["last_recoverable_point"], hour)

    def test_broken_parent_and_missing_object_fall_back(self):
        full = self.success("full")["point"]
        first = self.success("hourly")["point"]
        second = self.success("hourly")["point"]
        transport = self.transport()
        transport.delete(f"commits/{first}.json")
        state = self.success("status")
        self.assertEqual(state["last_recoverable_point"], full)
        self.assertTrue(state["degraded"])
        self.assertTrue(state["alert"])
        target = self.root / "must-remain-absent"
        status, _, _ = self.invoke("restore", second, "--snapshot-to", target)
        self.assertIn("chain is broken", status)
        self.assertFalse(target.exists())
        full_object = next(iter(transport.list_files("objects/" + full)))
        transport.delete(f"objects/{full}/{full_object}")
        self.assertIsNone(self.success("status")["last_recoverable_at"])

    def test_ciphertext_corruption_aborts_restore_without_publishing_target(self):
        full = self.success("full")["point"]
        blobs = [item for item in self.store.rglob("*") if item.is_file()]
        decoded = self.transport().list_files(f"objects/{full}")
        largest = max(decoded.values())
        candidates = [path for path in blobs if path.stat().st_size == remote_backup.ciphertext_size(largest)]
        self.assertTrue(candidates)
        for path in candidates:
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
        target = self.root / "incomplete"
        self.assertIsNotNone(self.invoke("restore", full, "--snapshot-to", target)[0])
        self.assertFalse(target.exists())

    def test_unrelated_corrupt_commit_does_not_block_exact_full_restore(self):
        old_full = self.success("full")["point"]
        original_blobs = self.encrypted_files()
        new_full = self.success("full")["point"]
        commit = f"{new_full}.json"
        listed = self.transport().list_files("commits")
        self.corrupt_ciphertext(self.encrypted_files() - original_blobs, listed[commit])
        self.assertEqual(self.transport().list_files("commits"), listed)
        with self.assertRaises(CryptError):
            self.transport().download(f"commits/{commit}", self.root / "unreadable-commit")

        target = self.root / "healthy-full"
        self.assertEqual(self.success("restore", old_full, "--snapshot-to", target)["point"], old_full)
        self.assertEqual(backup.verify(target)["stamp"], old_full)
        self.assertEqual((target / "catalog/unchanged.parquet").read_bytes(), self.stable.read_bytes())
        state = self.success("status")
        self.assertEqual(state["last_recoverable_point"], old_full)
        self.assertTrue(state["degraded"])

    def test_live_status_and_latest_restore_skip_equal_length_corruption_and_descendants(self):
        full = self.success("full")["point"]
        healthy_hour = self.success("hourly")["point"]
        original_blobs = self.encrypted_files()
        bad_payload = self.catalog / "later-corrupt.parquet"
        bad_payload.write_bytes(b"c" * 32768)
        bad_hour = self.success("hourly")["point"]
        bad_blobs = self.encrypted_files() - original_blobs
        descendant = self.success("hourly")["point"]
        self.assertEqual([p.id for p in self.read_inventory().chain(descendant)],
                         [full, healthy_hour, bad_hour, descendant])
        listing = self.transport().list_files(f"objects/{bad_hour}")
        self.corrupt_ciphertext(bad_blobs, bad_payload.stat().st_size)
        self.assertEqual(self.transport().list_files(f"objects/{bad_hour}"), listing)

        self.assertEqual(self.success("status", "--cached")["last_recoverable_point"], descendant)
        with self.subTest("latest before status refresh"):
            target = self.root / "latest-healthy"
            self.assertEqual(self.success("restore", "latest", "--snapshot-to", target)["point"], healthy_hour)
            self.assertEqual(backup.verify(target)["stamp"], healthy_hour)
            self.assertFalse((target / "catalog/later-corrupt.parquet").exists())

        with self.subTest("live status"):
            state = self.success("status")
            self.assertEqual(state["last_recoverable_point"], healthy_hour)
            self.assertEqual(state["last_hourly_at"], remote_backup.iso_point(healthy_hour))
            self.assertTrue(state["degraded"])
            self.assertTrue(state["alert"])
            self.assertEqual(self.success("status", "--cached")["last_recoverable_point"], healthy_hour)

        with self.subTest("explicit point"):
            exact = self.root / "explicit-corrupt"
            self.assertIsNotNone(self.invoke("restore", descendant, "--snapshot-to", exact)[0])
            self.assertFalse(exact.exists())

    def test_hourly_clears_cached_points_when_remote_archive_is_empty(self):
        full = self.success("full")["point"]
        hour = self.success("hourly")["point"]
        self.assertEqual(self.success("status", "--cached")["last_recoverable_point"], hour)
        transport = self.transport()
        transport.purge("commits")
        transport.purge("objects")

        self.assertIn("hourly backup requires a verified full recovery point", self.invoke("hourly")[0])
        state = self.success("status", "--cached")
        self.assertIsNone(state["last_recoverable_point"])
        self.assertIsNone(state["last_full_at"])
        self.assertIsNone(state["last_hourly_at"])
        self.assertTrue(state["degraded"])
        self.assertTrue(state["alert"])
        self.assertNotEqual(self.success("full")["point"], full)

    def test_hourly_removes_known_invalid_full_from_cached_schedule_markers(self):
        full = self.success("full")["point"]
        transport = self.transport()
        object_name = next(iter(transport.list_files(f"objects/{full}")))
        transport.delete(f"objects/{full}/{object_name}")

        self.assertIn("hourly backup requires a verified full recovery point", self.invoke("hourly")[0])
        state = self.success("status", "--cached")
        self.assertIsNone(state["last_recoverable_point"])
        self.assertIsNone(state["last_full_at"])
        self.assertTrue(state["degraded"])
        self.assertTrue(state["alert"])

    def test_hourly_uses_older_full_when_newest_full_has_corrupt_ciphertext(self):
        good = self.success("full")["point"]
        original_blobs = self.encrypted_files()
        later_payload = self.catalog / "later-full.parquet"
        later_payload.write_bytes(b"f" * 32768)
        bad = self.success("full")["point"]
        self.corrupt_ciphertext(self.encrypted_files() - original_blobs, later_payload.stat().st_size)
        self.assertEqual(self.success("status", "--cached")["last_recoverable_point"], bad)

        state = self.success("hourly")
        hour = state["point"]
        self.assertEqual(state["last_recoverable_point"], hour)
        self.assertEqual(state["last_full_at"], remote_backup.iso_point(good))
        self.assertTrue(state["degraded"])
        self.assertEqual([point.id for point in self.read_inventory().chain(hour)], [good, hour])
        target = self.root / "hour-from-good-full"
        self.success("restore", hour, "--snapshot-to", target)
        self.assertEqual((target / "catalog/later-full.parquet").read_bytes(), later_payload.read_bytes())

    def test_hourly_listing_outage_preserves_last_verified_point_with_alert(self):
        full = self.success("full")["point"]
        with patch.object(CryptRemote, "list_files", side_effect=CryptError("remote_error", "synthetic listing outage")):
            self.assertIn("remote_error", self.invoke("hourly")[0])
        state = self.success("status", "--cached")
        self.assertEqual(state["last_recoverable_point"], full)
        self.assertEqual(state["last_full_at"], remote_backup.iso_point(full))
        self.assertIsNone(state["last_hourly_at"])
        self.assertTrue(state["degraded"])
        self.assertTrue(state["alert"])
        self.assertFalse(self.success("hourly")["degraded"])

    def test_payload_read_outage_preserves_last_verified_point_with_alert(self):
        full = self.success("full")["point"]
        for action in ("status", "hourly"):
            with self.subTest(action=action):
                self.assertFalse(self.success("status")["degraded"])
                with patch.object(CryptRemote, "object_hashes",
                                  side_effect=CryptError("remote_error", "synthetic object read outage")):
                    code, lines, _ = self.invoke(action)
                self.assertIn("remote_error", code)
                cached = self.success("status", "--cached")
                self.assertEqual(cached["last_recoverable_point"], full)
                self.assertEqual(cached["last_full_at"], remote_backup.iso_point(full))
                self.assertTrue(cached["degraded"])
                self.assertTrue(cached["alert"])
                if action == "status":
                    self.assertEqual(json.loads(lines[0])["last_recoverable_point"], full)

    def test_missing_keys_and_capacity_fail_with_no_commit(self):
        full = self.success("full")["point"]
        with patch("backup_crypt.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            self.assertIn("capacity", self.invoke("hourly")[0])
        self.assertEqual(self.success("status")["last_recoverable_point"], full)
        self.config.unlink()
        code, lines, _ = self.invoke("status")
        self.assertIn("key_missing", code)
        state = json.loads(lines[0])
        self.assertEqual(state["last_recoverable_point"], full)
        self.assertTrue(state["alert"])

    def test_hourly_rebuilds_corrupt_local_status(self):
        self.success("full")
        remote_backup.state_path(self.box).write_text("{")
        self.assertTrue(self.success("status", "--cached")["alert"])
        hour = self.success("hourly")["point"]
        state = self.success("status", "--cached")
        self.assertEqual(state["last_recoverable_point"], hour)
        self.assertFalse(state["degraded"])

    def test_capacity_uses_ciphertext_size(self):
        self.assertEqual([remote_backup.ciphertext_size(n) for n in (0, 1, 65536, 65537)], [32, 49, 65584, 65601])
        with patch("backup_crypt.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            self.assertIn("capacity", self.invoke("full")[0])
        self.assertEqual(self.transport().list_files("commits"), {})

    def test_prune_guards_recent_restore_and_preserves_chain(self):
        now = datetime.now(UTC)
        week = now - timedelta(days=35 + now.weekday())
        ids = []
        for offset in (0, 1, 7, 14, 21, 28, 29, 30, 31, 32, 33, 34):
            ids.append(self.success("full", cut=(week + timedelta(days=offset)).strftime(backup.STAMP))["point"])
        hour = self.success("hourly")["point"]
        self.assertIn("successful isolated restore", self.invoke("prune")[0])
        found = self.read_inventory()
        proof = {"remote_id": remote_backup.reference_id("private:fixture"),
                 "restored_at": (now - timedelta(days=8)).strftime(backup.STAMP),
                 "point": found.points[hour].reference()}
        backup.atomic_json(remote_backup.receipt_file(self.box), proof)
        self.assertIn("successful isolated restore", self.invoke("prune")[0])
        self.assertEqual(self.read_inventory().points.keys(), found.points.keys())
        proof["restored_at"] = backup.utc_stamp()
        backup.atomic_json(remote_backup.receipt_file(self.box), proof)
        result = self.success("prune")
        self.assertEqual(result, {"pruned": 2, "retained": 11})
        remaining = self.read_inventory()
        # 7 daily fulls are in the newest week. Three older ISO weeks
        # contribute one full each; the oldest week drops both its fulls.
        self.assertEqual(remaining.points.keys(), {*ids[2:], hour})
        self.assertEqual([p.id for p in remaining.chain(hour)], [ids[-1], hour])
        self.success("restore", hour, "--snapshot-to", self.root / "after-prune")

    def test_prune_preserves_only_healthy_full_when_recently_restored_full_is_corrupt(self):
        now = datetime.now(UTC)
        old = self.success("full", cut=(now - timedelta(days=1)).strftime(backup.STAMP))["point"]
        original_blobs = self.encrypted_files()
        payload = self.catalog / "newest-full.parquet"
        payload.write_bytes(b"r" * 32768)
        recent = self.success("full", cut=now.strftime(backup.STAMP))["point"]
        found = self.read_inventory()
        self.assertEqual(remote_backup.keep_points(found, 1, 0, recent), {recent})
        backup.atomic_json(remote_backup.receipt_file(self.box), {
            "remote_id": remote_backup.reference_id("private:fixture"),
            "restored_at": backup.utc_stamp(),
            "point": found.points[recent].reference(),
        })

        transport = self.transport()
        commits = transport.list_files("commits")
        objects = transport.list_files("objects")
        self.corrupt_ciphertext(self.encrypted_files() - original_blobs, payload.stat().st_size)
        self.assertEqual(transport.list_files("commits"), commits)
        self.assertEqual(transport.list_files("objects"), objects)
        self.assertEqual(self.success("status")["last_recoverable_point"], old)

        with (patch.dict(os.environ, {"FERTILE_BACKUP_DAYS": "1", "FERTILE_BACKUP_WEEKS": "0"}),
              patch.object(CryptRemote, "delete", autospec=True, side_effect=CryptRemote.delete) as delete,
              patch.object(CryptRemote, "purge", autospec=True, side_effect=CryptRemote.purge) as purge):
            status, lines, _ = self.invoke("prune")
        self.assertIn("remote prune blocked by unreadable or corrupt retained recovery payload", status or "", lines)
        delete.assert_not_called()
        purge.assert_not_called()
        self.assertEqual(transport.list_files("commits"), commits)
        self.assertEqual(transport.list_files("objects"), objects)
        target = self.root / "only-healthy-full"
        self.assertEqual(self.success("restore", old, "--snapshot-to", target)["point"], old)
        self.assertEqual(backup.verify(target)["stamp"], old)

    def test_hourly_can_publish_while_full_upload_is_waiting(self):
        previous = self.success("full")["point"]
        transfer, release = Event(), Event()
        original = CryptRemote.upload_tree
        def blocking_upload(transport, source, destination):
            if not transfer.is_set():
                transfer.set()
                if not release.wait(15):
                    raise TimeoutError("hourly backup did not finish during full transfer")
            return original(transport, source, destination)
        def run(kind):
            with tempfile.TemporaryDirectory(dir=self.root) as work:
                remote_backup.run_backup(SimpleNamespace(action=kind, seal_wal=False), self.box,
                                         "private:fixture", self.transport(), Path(work))
        def pg_command(_box, *command, **_kwargs):
            if command[0] == "psql":
                return "t"
            Path(command[command.index("--file") + 1]).write_text("synthetic dump")
        with (patch.object(backup.Box, "command", pg_command), patch.object(CryptRemote, "upload_tree", blocking_upload),
              redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=2) as pool):
            pending_full = pool.submit(run, "full")
            try:
                self.assertTrue(transfer.wait(10))
                pool.submit(run, "hourly").result(timeout=12)
                self.assertFalse(pending_full.done())
            finally:
                release.set()
            pending_full.result(timeout=12)
        found = self.read_inventory()
        first_hour = found.latest("hourly")
        new_full = found.latest("full")
        self.assertEqual(found.chain(first_hour)[0].id, previous)
        self.assertEqual(self.success("status", "--cached")["last_recoverable_point"], first_hour)
        hour = self.success("hourly")["point"]
        self.assertEqual(self.read_inventory().chain(hour)[0].id, new_full)

    def test_hourly_completion_preserves_full_published_after_parent_scan(self):
        previous = self.success("full")["point"]
        listed_commits, release_hour = Event(), Event()
        original = CryptRemote.list_files

        def pause_hour(transport, relative):
            files = original(transport, relative)
            if relative == "commits" and not listed_commits.is_set():
                listed_commits.set()
                if not release_hour.wait(15):
                    raise TimeoutError("full did not publish during hourly scan")
            return files

        def run(kind):
            with tempfile.TemporaryDirectory(dir=self.root) as work:
                remote_backup.run_backup(SimpleNamespace(action=kind, seal_wal=False), self.box,
                                         "private:fixture", self.transport(), Path(work))

        def pg_command(_box, *command, **_kwargs):
            if command[0] == "psql":
                return "t"
            Path(command[command.index("--file") + 1]).write_text("synthetic dump")

        with (patch.object(backup.Box, "command", pg_command), patch.object(CryptRemote, "list_files", pause_hour),
              redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=2) as pool):
            pending_hour = pool.submit(run, "hourly")
            try:
                self.assertTrue(listed_commits.wait(10))
                pool.submit(run, "full").result(timeout=12)
                self.assertFalse(pending_hour.done())
            finally:
                release_hour.set()
            pending_hour.result(timeout=12)
        found = self.read_inventory()
        new_full = found.latest("full")
        hour = found.latest("hourly")
        self.assertEqual([point.id for point in found.chain(hour)], [previous, hour])
        state = self.success("status", "--cached")
        self.assertEqual(state["last_recoverable_point"], max(new_full, hour))
        self.assertEqual(state["last_full_at"], remote_backup.iso_point(new_full))
        self.assertEqual(state["last_hourly_at"], remote_backup.iso_point(hour))

    def test_alert_after_one_hour_even_if_integer_age_rounds_down(self):
        point = "20261127T030000.000000Z"
        state = {"last_point": point}
        start = remote_backup.point_time(point)
        self.assertFalse(remote_backup.status_fields(state, start + timedelta(hours=1))["alert"])
        self.assertTrue(remote_backup.status_fields(state, start + timedelta(hours=1, microseconds=1))["alert"])
        self.assertTrue(remote_backup.status_fields({}, start)["alert"])


if __name__ == "__main__":
    unittest.main()
