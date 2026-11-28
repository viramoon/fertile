"""Regression tests for the T7 runner and its isolation boundary."""

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from e2e_smoke import SmokeError
import t3b_integration as harness
import t7_backtest_acceptance as t7


class IsolatedAcceptance(unittest.TestCase):
    def test_cli_refuses_caller_supplied_targets(self):
        for option, value in {
            "--data-root": "/decoy/data-paper",
            "--research-root": "/decoy/data/research",
            "--database-url": "postgresql://localhost/web",
            "--web-origin": "http://127.0.0.1:8090",
            "--port": "8090",
            "--rele": "/decoy",
        }.items():
            with (self.subTest(option=option), patch.object(t7, "run") as run,
                  redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised):
                t7.main(["--release", "/tmp", option, value])
            self.assertEqual(raised.exception.code, 2)
            run.assert_not_called()

    def test_reserved_listener_is_rejected(self):
        with patch.object(t7, "ephemeral_port", return_value=8090):
            with self.assertRaisesRegex(SmokeError, "reserved port"):
                t7.isolated_port()

    def test_mismatched_candidate_is_rejected_before_startup(self):
        with (tempfile.TemporaryDirectory(dir="/tmp") as directory,
              patch.object(t7, "run") as run):
            (Path(directory) / "manifest.json").write_text('{"components": {}}', encoding="utf-8")
            with self.assertRaisesRegex(SmokeError, "differ from this checkout"):
                t7.main(["--release", directory])
            run.assert_not_called()

    def test_run_creates_private_sibling_roots_and_cleans_up_on_failure(self):
        def fail(_release, work, data, research, _source, _upstream, _http, _token):
            self.assertEqual(work.parent, Path("/tmp").resolve())
            self.assertEqual(work.stat().st_mode & 0o777, 0o700)
            for root in (data, research):
                self.assertEqual(root.parent, work)
                self.assertTrue(root.is_dir())
                self.assertEqual(list(root.iterdir()), [])
            self.assertNotEqual(data, research)
            raise SmokeError("synthetic assertion failure")

        with (patch.dict(os.environ, FERTILE_DATA_ROOT="/decoy/data-paper",
                         FERTILE_RESEARCH_ROOT="/decoy/data/research", TMPDIR="/decoy"),
              patch.object(t7, "archive_finance"),
              patch.object(t7, "isolated_port", return_value=34567),
              patch.object(t7, "postgres") as postgres,
              patch.object(t7, "finance_api"),
              patch.object(t7, "gateway") as gateway,
              patch.object(t7, "exercise_backtests", side_effect=fail)):
            gateway.return_value.__enter__.return_value = (Mock(), "synthetic-test-token")
            with self.assertRaisesRegex(SmokeError, "synthetic assertion failure"):
                t7.run(Path("/candidate"), "fixture-pin")
            work = postgres.call_args.args[0]
            postgres.return_value.__exit__.assert_called_once()
            gateway.return_value.__exit__.assert_called_once()
            self.assertFalse(work.exists())

    def test_gateway_and_database_children_only_receive_private_targets(self):
        live = "postgresql://localhost/web"
        with (tempfile.TemporaryDirectory(dir="/tmp") as directory,
              patch.dict(os.environ, DATABASE_URL=live, PGHOST="/decoy/pg", PGDATABASE="web",
                         WEB_PORT="8090", FINANCE_BASE_URL="http://127.0.0.1:8081",
                         HTTP_PROXY="http://127.0.0.1:8081", WEB_ADMIN_TOKEN_FILE="/decoy/token"),
              patch.object(harness.subprocess, "run") as spawn,
              patch.object(harness, "child") as child,
              patch.object(harness, "HTTP") as http):
            work = Path(directory)
            finance_origin = "http://127.0.0.1:34568"
            http.return_value.read.return_value = {"status": "ready"}
            with (harness.postgres(work) as database,
                  harness.gateway(Path("/candidate"), work, database, finance_origin, port=34567)):
                self.assertEqual(database.split("?")[0], "postgresql://t3b@localhost/t3b")
                init, start, createdb, migrate = spawn.call_args_list
                self.assertIn("--auth-host=reject", init.args[0])
                self.assertIn(f"-h '' -k '{work / 'pg'}' -p 5432", start.args[0])
                self.assertEqual(createdb.args[0], ("createdb", "t3b"))
                self.assertEqual(createdb.kwargs["env"]["PGHOST"], str(work / "pg"))
                self.assertNotIn("PGDATABASE", createdb.kwargs["env"])
                self.assertEqual(migrate.kwargs["env"], child.call_args.args[2])
                web_env = migrate.kwargs["env"]
                self.assertEqual(web_env["DATABASE_URL"], database)
                self.assertEqual(web_env["FINANCE_BASE_URL"], finance_origin)
                self.assertEqual(web_env["WEB_PORT"], "34567")
                self.assertEqual(Path(web_env["WEB_ADMIN_TOKEN_FILE"]).parent, work)
                self.assertEqual(web_env["HOME"], str(work))
                self.assertNotIn("HTTP_PROXY", web_env)
                http.assert_called_once_with("http://127.0.0.1:34567", local=True)

    def test_private_id_in_anonymous_continuation_fails(self):
        public = {"id": 1, "request": {"strategy": "SmaCrossover"}}
        last_public = {"id": 112, "request": {"strategy": "SmaCrossover"}}
        private_ids = list(range(2, 112)) + [113]
        first = {"items": [public], "next_cursor": 1}
        for pages, error in (
            ([{**first, "next_cursor": 2}], "cursor did not identify"),
            ([first, {"items": [last_public], "next_cursor": 113}], "private tail"),
        ):
            with self.subTest(error=error):
                http = Mock()
                http.read.side_effect = pages
                with self.assertRaisesRegex(SmokeError, error):
                    t7.check_anonymous_pagination(http, [1, 112], private_ids)

    def test_anonymous_uses_each_public_cursor_and_checks_the_private_tail(self):
        http = Mock()
        http.read.side_effect = [
            {"items": [{"id": job_id, "request": {"strategy": "SmaCrossover"}}], "next_cursor": cursor}
            for job_id, cursor in ((1, 1), (112, 112), (113, None))
        ] + [
            {"items": [], "next_cursor": None},
            *({"error": {"code": "job_not_found"}} for _ in range(3)),
        ]
        with patch.object(t7, "log"):
            t7.check_anonymous_pagination(http, [1, 112, 113], [*range(2, 112), *range(114, 122)])
        self.assertEqual(http.read.call_args_list, [
            call("/api/v1/backtests?limit=1"),
            call("/api/v1/backtests?limit=1&cursor=1"),
            call("/api/v1/backtests?limit=1&cursor=112"),
            call("/api/v1/backtests?limit=1&cursor=113"),
            call("/api/v1/backtests/2", expected=404),
            call("/api/v1/backtests/111", expected=404),
            call("/api/v1/backtests/121", expected=404),
        ])

    @unittest.skipUnless(os.environ.get("FERTILE_T7_TEST_RELEASE"), "candidate artifact not supplied")
    def test_real_replay_and_pages_ignore_inherited_live_targets(self):
        release = Path(os.environ["FERTILE_T7_TEST_RELEASE"]).resolve(strict=True)
        with (tempfile.TemporaryDirectory(prefix="fertile-t7-decoy.", dir="/tmp") as directory,
              socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as database,
              socket.socket(socket.AF_INET, socket.SOCK_STREAM) as http):
            decoy = Path(directory)
            for name in ("data-paper", "data/research", "pg-live", "config", "tmp"):
                (decoy / name).mkdir(parents=True)
            markers = ("data-paper/marker", "data/research/marker", "config/marker")
            for name in markers:
                (decoy / name).write_text("synthetic decoy\n", encoding="utf-8")
            database.bind(str(decoy / "pg-live/.s.PGSQL.5432"))
            http.bind(("127.0.0.1", 0))
            for listener in (database, http):
                listener.listen()
                listener.setblocking(False)
            origin = f"http://127.0.0.1:{http.getsockname()[1]}"
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(decoy),
                "TMPDIR": str(decoy / "tmp"),
                "FERTILE_ROOT": str(decoy),
                "FERTILE_DATA_ROOT": str(decoy / "data-paper"),
                "FERTILE_RESEARCH_ROOT": str(decoy / "data/research"),
                "DATABASE_URL": f"postgresql://localhost/web?host={decoy / 'pg-live'}",
                "PGHOST": str(decoy / "pg-live"),
                "PGPORT": "5432",
                "PGDATABASE": "web",
                "PGCONNECT_TIMEOUT": "1",
                "FINANCE_BASE_URL": origin,
                "WEB_CONFIG_DIR": str(decoy / "config"),
                "WEB_PORT": str(http.getsockname()[1]),
                "HTTP_PROXY": origin,
                "ALL_PROXY": origin,
                "NO_PROXY": "",
            }
            before = {path.relative_to(decoy) for path in decoy.rglob("*")}
            with subprocess.Popen(
                [sys.executable, "-EB", t7.__file__, "--release", release],
                cwd=t7.ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            ) as process:
                try:
                    output, _ = process.communicate(timeout=180)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        output, _ = process.communicate(timeout=60)
                    finally:
                        if process.poll() is None:
                            process.kill()
                    self.fail(f"T7 did not finish before the deadline\n{output}")
            self.assertEqual(process.returncode, 0, output)
            self.assertIn("T7 isolated replay submission and result readback passed", output)
            self.assertIn("T7 anonymous pagination passed", output)
            self.assertEqual({path.relative_to(decoy) for path in decoy.rglob("*")}, before)
            for name in markers:
                self.assertEqual((decoy / name).read_text(encoding="utf-8"), "synthetic decoy\n")
            for listener in (database, http):
                with self.assertRaises(BlockingIOError):
                    listener.accept()


if __name__ == "__main__":
    unittest.main()
