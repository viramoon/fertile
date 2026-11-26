"""Offline process drill with synthetic fixtures and a fixed sequence-based price formula."""

import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hot_upgrade as upgrade


OLD = "1" * 40
NEW = "2" * 40
STREAM = "replay/synthetic"


def wait_for(check):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.01)
    raise TimeoutError("synthetic fixture step did not finish")


def publish(path, value):
    temporary = path.with_name(f".{path.name}-{os.getpid()}")
    temporary.write_text(str(value))
    temporary.replace(path)


def read_int(path):
    try:
        return int(path.read_text())
    except FileNotFoundError:
        return 0


def writer(root, version):
    # The fixture holds a kernel lease shared by releases of this source.
    base = root / "fixture"
    with (base / "writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(75)
        with (base / "wal").open("a+b") as output:
            seq = read_int(base / "durable.seq")
            output.seek(0)
            rows = output.readlines()
            assert len(rows) >= seq and all(row.endswith(b"\n") for row in rows[:seq])
            output.truncate(sum(map(len, rows[:seq])))
            while True:
                if (base / f"pause-{version}").exists():
                    publish(base / f"paused-{version}.seq", seq)
                    if (base / f"partial-{version}").exists():
                        output.write(f"{seq + 1},partial".encode())
                        output.flush()
                        os.fsync(output.fileno())
                        publish(base / f"partial-{version}-written", 1)
                        signal.pause()
                    time.sleep(.01)
                    continue
                seq += 1
                # Reproducible synthetic cents from the sequence alone, without floats.
                output.write(f"{seq},{100 + seq // 10}.{seq % 10}00\n".encode())
                output.flush()
                os.fsync(output.fileno())
                publish(base / "durable.seq", seq)
                publish(base / f"writer-{version}.seq", seq)
                time.sleep(.01)


def query_schema(base, version):
    with closing(sqlite3.connect(base / "projection.sqlite")) as connection:
        columns = "price" if version == "v1" else "price, observation"
        return connection.execute(f"SELECT {columns} FROM projected ORDER BY seq").fetchall()


def reader(root, version):
    base = root / "fixture"
    while True:
        seq = read_int(base / "durable.seq")
        rows = (base / "wal").read_text().splitlines()
        assert len(rows) >= seq
        projected = query_schema(base, version)
        if seq:
            assert int(rows[seq - 1].partition(",")[0]) == seq
            publish(base / f"reader-{version}.json", json.dumps({"position": seq, "projection": projected}))
        time.sleep(.02)


def crash_controller(root, writer_pid):
    base = root / "fixture"

    def invoke(step, request):
        if step == "compatibility":
            return {"compatible": True}
        if step == "collector-seal":
            publish(base / "pause-v1", 1)
            seq = wait_for(lambda: read_int(base / "paused-v1.seq"))
            return {"watermark": {STREAM: str(seq)}}
        if step == "collector-release":
            publish(base / "partial-v1", 1)
            wait_for(lambda: (base / "partial-v1-written").exists())
            os.kill(writer_pid, signal.SIGKILL)
            publish(base / "release-interrupted", 1)
            signal.pause()
        raise AssertionError(f"unexpected fixture step {step}")

    upgrade.perform(root, invoke, service="finance-collector-replay", role="collector",
                    previous=OLD, target=NEW, source="replay")


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="fertile-upgrade-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "fixture"
        self.base.mkdir()

    def launch(self, action, argument):
        process = subprocess.Popen([sys.executable, "-B", __file__, "fixture", action,
                                    str(self.root), str(argument)], stdin=subprocess.DEVNULL)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def run_reader(self, hook, **overrides):
        arguments = dict(service="finance-api", role="reader", previous=OLD, target=NEW)
        arguments.update(overrides)
        return upgrade.perform(self.root, hook, **arguments)

    def run_collector(self, hook):
        return self.run_reader(hook, service="finance-collector-replay", role="collector", source="replay")

    def fixture_response(self):
        version = (self.base / "active-reader").read_text()
        try:
            return json.loads((self.base / f"reader-{version}.json").read_text())
        except FileNotFoundError:
            return None

    def test_api_upgrade_keeps_collector_running_and_older_reader_can_read_new_data(self):
        database = self.base / "projection.sqlite"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE projected (seq INTEGER PRIMARY KEY, price TEXT NOT NULL)")
            connection.execute("INSERT INTO projected VALUES (1, '102.0100')")
        collector = self.launch("writer", "v1")
        before = wait_for(lambda: read_int(self.base / "durable.seq"))
        processes = {"v1": self.launch("reader", "v1")}
        publish(self.base / "active-reader", "v1")
        self.assertEqual(wait_for(self.fixture_response)["projection"], [["102.0100"]])

        version_by_sha = {OLD: "v1", NEW: "v2"}

        def hook(step, request):
            selected = version_by_sha[request["previous"]]
            candidate = version_by_sha[request["target"]]
            self.assertIsNone(collector.poll())
            if step == "compatibility":
                self.assertEqual(query_schema(self.base, "v1")[0][0], "102.0100")
                return {"compatible": True}
            if step == "migrate":
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("ALTER TABLE projected ADD COLUMN observation TEXT")
                    connection.execute("INSERT INTO projected VALUES (2, '103.0150', 'synthetic')")
                return {"expanded": True}
            if step == "reader-checkpoint":
                return {"watermark": {STREAM: str(read_int(self.base / "durable.seq"))}}
            if step == "candidate-start":
                (self.base / f"reader-{candidate}.json").unlink(missing_ok=True)
                processes[candidate] = self.launch("reader", candidate)
            elif step in {"candidate-ready", "reader-ready"}:
                if step == "candidate-ready":
                    old_at_start = read_int(self.base / "durable.seq")
                    wait_for(lambda: read_int(self.base / "durable.seq") >= old_at_start + 2)
                    self.assertIsNone(processes[selected].poll())
                    self.assertEqual((self.base / "active-reader").read_text(), selected)
                else:
                    self.assertEqual((self.base / "active-reader").read_text(), candidate)
                content = self.base / f"reader-{candidate}.json"
                wait_for(content.exists)
                return {"ready": True, "watermark": {STREAM: str(json.loads(content.read_text())["position"])}}
            elif step == "reader-cutover":
                publish(self.base / "active-reader", candidate)
                self.stop(processes[selected])
            else:
                self.fail(f"unexpected fixture step {step}")
            return {}

        result = self.run_reader(hook, migrate=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.fixture_response()["projection"], [["102.0100", None], ["103.0150", "synthetic"]])
        rolled_back = self.run_reader(hook, previous=NEW, target=OLD, direction="rollback")
        self.assertEqual(rolled_back["status"], "complete")
        response = self.fixture_response()
        self.assertEqual(response["projection"], [["102.0100"], ["103.0150"]])
        self.assertGreater(response["position"], before)
        self.assertIsNone(collector.poll())
        self.assertTrue((self.base / "writer-v1.seq").exists())
        self.assertFalse((self.base / "writer-v2.seq").exists())

    def test_collector_releases_before_replacement_and_records_source_gap(self):
        old = self.launch("writer", "v1")
        wait_for(lambda: read_int(self.base / "writer-v1.seq"))
        competing = self.launch("writer", "v2")
        self.assertEqual(competing.wait(timeout=10), 75)
        replacement = None

        def hook(step, request):
            nonlocal replacement
            journal = upgrade.records(self.root)[0]
            self.assertEqual((journal["step"], journal["step_state"]), (step, "intent"))
            if step == "compatibility":
                return {"compatible": True}
            if step == "collector-seal":
                self.assertEqual(journal["gap"]["continuity"], "unknown")
                publish(self.base / "pause-v1", 1)
                seq = wait_for(lambda: read_int(self.base / "paused-v1.seq"))
                return {"watermark": {STREAM: str(seq)}}
            if step == "collector-release":
                self.stop(old)
                with (self.base / "writer.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return {"released": True, "watermark": request["evidence"]["collector-seal"]["watermark"]}
            if step == "collector-start":
                self.assertIsNotNone(old.poll())
                replacement = self.launch("writer", "v2")
                return {}
            if step == "collector-ready":
                seq = wait_for(lambda: read_int(self.base / "writer-v2.seq"))
                return {"ready": True, "watermark": {STREAM: str(seq)}}
            if step == "collector-gap":
                self.assertIsNone(replacement.poll())
                # Fixed synthetic source scenario: the gap lacks original ticks.
                return {"continuity": "bars-restored", "missing_count": "2"}
            self.fail(f"unexpected fixture step {step}")

        record = self.run_collector(hook)
        gap = upgrade.records(self.root)[0]["gap"]
        self.assertEqual(record["status"], "complete")
        self.assertEqual((gap["source"], gap["continuity"], gap["missing_count"]), ("replay", "bars-restored", "2"))
        self.assertLessEqual(gap["opened_at"], gap["ready_at"])

    def test_collector_gap_records_distinct_product_outcomes(self):
        other = "replay/synthetic-other"
        # Consecutive transport observations around a synthetic interruption.
        updates = {STREAM: (100, 103), other: (1, 9007199254740995)}
        methods = {STREAM: "bars-restored", other: "unrecoverable"}
        outcomes = {
            stream: {"continuity": methods[stream], "missing_count": str(after - before - 1)}
            for stream, (before, after) in updates.items()
        }
        pending = {stream: {"continuity": "unknown", "missing_count": None} for stream in updates}

        def hook(step, request):
            if step == "collector-gap":
                self.assertEqual(request["gap"]["products"], pending)
                self.assertEqual(upgrade.records(self.root)[0]["gap"]["products"], pending)
                return {"products": outcomes}
            return {
                "compatibility": {"compatible": True},
                "collector-seal": {"watermark": {STREAM: "7"}},
                "collector-release": {"released": True, "watermark": {STREAM: "7", other: "2"}},
                "collector-start": {},
                "collector-ready": {"ready": True, "watermark": {STREAM: "8", other: "3"}},
            }[step]

        record = self.run_collector(hook)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["gap"]["products"], outcomes)
        self.assertEqual(record["evidence"]["collector-gap"]["products"], outcomes)
        self.assertEqual(upgrade.records(self.root)[0]["gap"]["products"][other]["missing_count"], "9007199254740993")
        self.assertEqual((record["gap"]["continuity"], record["gap"]["missing_count"]), ("unknown", None))

    def test_collector_gap_rejects_unscoped_incomplete_or_unverified_results(self):
        other = "replay/synthetic-other"
        unknown = {"continuity": "unknown", "missing_count": None}
        unverified = {**unknown, "missing_count": "2"}
        both = {STREAM: "7", other: "4"}
        cases = (
            ({STREAM: "7"}, unverified),
            (both, {"continuity": "bars-restored", "missing_count": "2"}),
            (both, {"products": {STREAM: unknown}}),
            (both, {"products": {STREAM: unknown, other: unverified}}),
            (both, {"products": {STREAM: unknown, other: unknown, "ibkr/synthetic": unknown}}),
        )
        for index, (watermark, outcome) in enumerate(cases):
            with self.subTest(outcome=outcome):
                root = self.root / str(index)
                responses = {
                    "compatibility": {"compatible": True},
                    "collector-seal": {"watermark": watermark},
                    "collector-release": {"released": True, "watermark": watermark},
                    "collector-start": {},
                    "collector-ready": {"ready": True, "watermark": watermark},
                    "collector-gap": outcome,
                }
                with self.assertRaisesRegex(RuntimeError, "failed at collector-gap"):
                    upgrade.perform(root, lambda step, _: responses[step], service="finance-collector-replay",
                                    role="collector", previous=OLD, target=NEW, source="replay")
                record, = upgrade.records(root)
                self.assertEqual(record["status"], "needs-reconcile")
                self.assertEqual(record["gap"]["products"], dict.fromkeys(watermark, unknown))
                self.assertIsNone(record["gap"]["missing_count"])
                self.assertNotIn("collector-gap", record["evidence"])

    def test_collector_gap_reads_legacy_source_only_journal(self):
        transaction_id = "1" * 32
        directory = self.root / upgrade.JOURNAL
        directory.mkdir(parents=True)
        legacy = {
            "version": 1, "id": transaction_id, "status": "complete", "started_at": "2026-01-01T00:00:00+00:00",
            "gap": {"source": "replay", "continuity": "bars-restored", "missing_count": "2"},
        }
        path = directory / f"{transaction_id}.json"
        path.write_text(json.dumps(legacy))
        self.assertEqual(upgrade.records(self.root), [legacy])
        self.assertEqual(json.loads(path.read_text()), legacy)

    def test_sigkill_partial_tail_leaves_unknown_gap_and_explicit_reconciliation(self):
        old = self.launch("writer", "v1")
        wait_for(lambda: read_int(self.base / "writer-v1.seq"))
        controller = self.launch("crash", old.pid)
        wait_for(lambda: (self.base / "release-interrupted").exists())
        self.assertEqual(old.wait(timeout=10), -signal.SIGKILL)
        controller.kill()
        self.assertEqual(controller.wait(timeout=10), -signal.SIGKILL)
        record, = upgrade.records(self.root)
        self.assertEqual((record["status"], record["step"], record["step_state"]),
                         ("running", "collector-release", "intent"))
        self.assertEqual(record["gap"]["continuity"], "unknown")
        self.assertIsNone(record["gap"]["ready_at"])
        checkpoint = int(record["evidence"]["collector-seal"]["watermark"][STREAM])
        self.assertTrue((self.base / "wal").read_bytes().endswith(b",partial"))
        with self.assertRaisesRegex(RuntimeError, "unfinished hot-upgrade"):
            self.run_collector(lambda *_: self.fail("unresolved operation allowed another handoff"))

        replacement = self.launch("writer", "v2")
        wait_for(lambda: read_int(self.base / "writer-v2.seq") > checkpoint)
        rows = (self.base / "wal").read_bytes().splitlines()
        self.assertEqual(int(rows[checkpoint].partition(b",")[0]), checkpoint + 1)
        self.assertNotIn(b"partial", b"".join(rows))

        def verify(step, request):
            self.assertEqual((step, request["interrupted_step"]), ("reconcile", "collector-release"))
            self.assertIsNone(replacement.poll())
            with (self.base / "writer.lock").open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {"reconciled": True, "active_release": NEW}

        finished = upgrade.reconcile(self.root, record["id"], verify)
        self.assertEqual((finished["status"], finished["gap"]["continuity"]), ("reconciled", "unknown"))
        with upgrade.lock(self.root):
            upgrade.require_clear(self.root)

    def test_projector_lag_aborts_only_the_candidate(self):
        steps = []
        responses = {
            "compatibility": {"compatible": True, "irrelevant": "not-journaled"},
            "reader-checkpoint": {"watermark": {STREAM: "9007199254740993"}},
            "candidate-ready": {"ready": True, "watermark": {STREAM: "9007199254740992"}},
        }

        def hook(step, _request):
            steps.append(step)
            return responses.get(step, {})

        with self.assertRaisesRegex(RuntimeError, "failed at candidate-ready"):
            self.run_reader(hook, service="finance-projector", role="projector")
        record, = upgrade.records(self.root)
        self.assertEqual(record["status"], "aborted")
        self.assertNotIn("reader-cutover", steps)
        self.assertEqual(steps[-1], "candidate-abort")
        self.assertEqual(record["evidence"]["compatibility"], {"compatible": True})
        self.assertEqual(record["evidence"]["reader-checkpoint"]["watermark"][STREAM], "9007199254740993")

    def test_failed_cutover_needs_confirmed_reconciliation(self):
        def hook(step, _request):
            match step:
                case "compatibility":
                    return {"compatible": True}
                case "reader-checkpoint" | "candidate-ready":
                    return {"ready": True, "watermark": {}}
                case "candidate-start":
                    return {}
                case "reader-cutover":
                    raise OSError("synthetic cutover failure")
                case _:
                    self.fail(f"unexpected fixture step {step}")

        with self.assertRaisesRegex(RuntimeError, "failed at reader-cutover"):
            self.run_reader(hook)
        record, = upgrade.records(self.root)
        self.assertEqual(record["status"], "needs-reconcile")
        with self.assertRaisesRegex(RuntimeError, "could not be reconciled"):
            upgrade.reconcile(self.root, record["id"], lambda *_: {"reconciled": True, "active_release": "3" * 40})
        done = upgrade.reconcile(self.root, record["id"], lambda *_: {"reconciled": True, "active_release": OLD})
        self.assertEqual(done["interrupted_step"], "reader-cutover")
        self.assertEqual(done["status"], "reconciled")

    def test_canonical_reader_must_reach_checkpoint_after_cutover(self):
        steps = []
        responses = {
            "compatibility": {"compatible": True},
            "reader-checkpoint": {"watermark": {STREAM: "7"}},
            "candidate-ready": {"ready": True, "watermark": {STREAM: "8"}},
            "reader-ready": {"ready": True, "watermark": {STREAM: "6"}},
        }

        def hook(step, _request):
            steps.append(step)
            return responses.get(step, {})

        with self.assertRaisesRegex(RuntimeError, "failed at reader-ready"):
            self.run_reader(hook, service="finance-projector", role="projector")
        self.assertEqual(upgrade.records(self.root)[0]["status"], "needs-reconcile")
        self.assertEqual(steps[-2:], ["reader-cutover", "reader-ready"])

    def test_release_evidence_must_confirm_both_lease_and_watermark(self):
        for index, release in enumerate((
                {"released": False, "watermark": {STREAM: "2"}},
                {"released": True, "watermark": {STREAM: "1"}},
                {"released": True, "watermark": {"ibkr/synthetic": "2"}},
        )):
            with self.subTest(release=release):
                root = self.root / str(index)
                steps = []

                def hook(step, _request):
                    steps.append(step)
                    match step:
                        case "compatibility":
                            return {"compatible": True}
                        case "collector-seal":
                            return {"watermark": {STREAM: "2"}}
                        case "collector-release":
                            return release
                        case _:
                            self.fail(f"unexpected fixture step {step}")

                with self.assertRaisesRegex(RuntimeError, "failed at collector-release"):
                    upgrade.perform(root, hook, service="finance-collector-replay", role="collector",
                                    previous=OLD, target=NEW, source="replay")
                self.assertNotIn("collector-start", steps)
                self.assertEqual(upgrade.records(root)[0]["gap"]["continuity"], "unknown")

    def test_worker_stops_only_after_drain_confirmation(self):
        steps = []

        def hook(step, _request):
            steps.append(step)
            return {"compatible": True, "drained": True, "ready": True}

        done = self.run_reader(hook, service="finance-backtest-worker", role="worker")
        self.assertEqual(done["status"], "complete")
        self.assertEqual(steps, ["compatibility", "worker-drain", "worker-stop", "worker-start", "worker-ready"])
        steps.clear()

        def refuses_drain(step, _request):
            steps.append(step)
            return {"compatible": True, "drained": False}

        with self.assertRaisesRegex(RuntimeError, "failed at worker-drain"):
            self.run_reader(refuses_drain, service="finance-backtest-worker", role="worker")
        self.assertEqual(steps, ["compatibility", "worker-drain"])

    def test_rollback_rejects_incompatible_data_and_disallows_migration(self):
        steps = []

        def hook(step, _request):
            steps.append(step)
            return {"compatible": False}

        with self.assertRaisesRegex(ValueError, "direction or migration"):
            self.run_reader(hook, previous=NEW, target=OLD, direction="rollback", migrate=True)
        self.assertEqual(upgrade.records(self.root), [])
        with self.assertRaisesRegex(RuntimeError, "failed at compatibility"):
            self.run_reader(hook, previous=NEW, target=OLD, direction="rollback")
        self.assertEqual(steps, ["compatibility"])
        self.assertEqual(upgrade.records(self.root)[0]["status"], "aborted")

    def test_shared_control_lock_and_restore_guard(self):
        marker = self.root / "run/restore-journal.json"
        marker.parent.mkdir()
        marker.write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "unfinished restore"):
            self.run_reader(lambda *_: self.fail("restore guard allowed handoff"))
        self.assertEqual(upgrade.records(self.root), [])
        marker.unlink()

        def hook(step, _request):
            self.assertEqual(step, "compatibility")
            with (self.root / "run/control.lock").open("a") as stream:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {"compatible": False}

        with self.assertRaisesRegex(RuntimeError, "failed at compatibility"):
            self.run_reader(hook)

    def test_display_only_collector_handoff_keeps_empty_watermarks(self):
        def hook(step, _request):
            return {"compatible": True, "display_only": True, "watermark": {},
                    "released": True, "ready": True, "continuity": "unknown"}

        record = self.run_collector(hook)
        self.assertTrue(record["evidence"]["collector-seal"]["display_only"])
        self.assertIsNone(record["gap"]["missing_count"])

    def test_display_only_collector_can_classify_a_product_without_watermarks(self):
        outcomes = {STREAM: {"continuity": "snapshot-only", "missing_count": None}}

        def hook(step, _request):
            if step == "collector-gap":
                return {"products": outcomes}
            return {"compatible": True, "display_only": True, "watermark": {}, "released": True, "ready": True}

        record = self.run_collector(hook)
        self.assertEqual(record["gap"]["products"], outcomes)
        self.assertEqual(record["evidence"]["collector-ready"]["watermark"], {})


if __name__ == "__main__":
    if sys.argv[1:2] == ["fixture"]:
        _, _, action, root, argument = sys.argv
        if action == "crash":
            crash_controller(Path(root), int(argument))
        else:
            {"reader": reader, "writer": writer}[action](Path(root), argument)
    else:
        unittest.main()
