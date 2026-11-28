"""Run the T3b gateway checks with a pinned release and temporary synthetic state."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlencode

from e2e_smoke import HTTP, SmokeError, require
from published_queue_readback import ephemeral_port
from t3b_market import build_owner, check_market, running_owner
from t3b_strategy import exercise_strategy, publish_strategy_replay


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("finance", "web", "frontend")


def verify_release(release):
    def revision(ref):
        return subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()

    manifest = json.loads((release / "manifest.json").read_bytes())
    pins = {name: revision(f"HEAD:{name}") for name in COMPONENTS}
    require(manifest["fertile"] == revision("HEAD") and manifest["components"] == pins,
            "T3b requires the release built from this fertile commit and its locked gitlinks")


def local_env():
    return {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}


@contextmanager
def postgres(work):
    socket_dir = work / "pg"
    socket_dir.mkdir()
    pgdata = work / "pgdata"
    env = {**local_env(), "PGHOST": str(socket_dir), "PGPORT": "5432", "PGUSER": "t3b"}

    def run(*command):
        subprocess.run(command, cwd=work, env=env, check=True, timeout=45)

    run("initdb", "--no-sync", "--no-locale", "--encoding=UTF8", "--auth-local=trust",
        "--auth-host=reject", "--username=t3b", "-D", pgdata)
    try:
        run("pg_ctl", "-D", pgdata, "-l", work / "postgres.log", "-w", "-t", "20", "start",
            "-o", f"-h '' -k '{socket_dir}' -p {env['PGPORT']}")
        run("createdb", "t3b")
        query = urlencode({"host": str(socket_dir), "port": env["PGPORT"]})
        yield f"postgresql://t3b@localhost/t3b?{query}"
    finally:
        if (pgdata / "postmaster.pid").exists():
            run("pg_ctl", "-D", pgdata, "-m", "immediate", "-w", "-t", "20", "stop")


@contextmanager
def child(command, work, env, name, ready):
    log = work / f"{name}.log"
    with log.open("xb") as output:
        process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 25
            while process.poll() is None:
                if ready(log.read_bytes()):
                    break
                require(time.monotonic() < deadline, f"{name} startup timed out")
                time.sleep(0.05)
            else:
                raise RuntimeError(f"{name} exited during startup (status {process.returncode})")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError(f"{name} did not shut down") from None
        require(process.returncode == 0, f"{name} exited with status {process.returncode}")


def web_listening(log, port):
    for line in log.splitlines():
        try:
            fields = json.loads(line).get("fields", {})
        except json.JSONDecodeError:
            continue
        if fields.get("message") == "listening" and fields.get("address") == f"127.0.0.1:{port}":
            return True
    return False


@contextmanager
def gateway(release, work, database, finance_origin):
    port = ephemeral_port()
    token = secrets.token_hex(32)
    token_file = work / "web-token"
    with os.fdopen(os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(f"WEB_ADMIN_TOKEN={token}\n")
    env = {
        **local_env(),
        "DATABASE_URL": database,
        "FERTILE_PROFILE": "stable",
        "FINANCE_BASE_URL": finance_origin,
        "TOKIO_WORKER_THREADS": "2",
        "WEB_ADMIN_TOKEN_FILE": str(token_file),
        "WEB_AUTO_MIGRATE": "false",
        "WEB_BIND_HOST": "127.0.0.1",
        "WEB_BINANCE_PROBES_ENABLED": "false",
        "WEB_CONFIG_DIR": str(release / "web/config"),
        "WEB_PORT": str(port),
        "WEB_REQUIRE_FINANCE": "true",
        "WEB_STATIC_ROOT": str(release / "static"),
    }
    binary = release / "bin/web"
    subprocess.run([binary, "migrate"], cwd=work, env=env, check=True, timeout=30)
    with child([binary, "serve"], work, env, "web", lambda log: web_listening(log, port)):
        http = HTTP(f"http://127.0.0.1:{port}", local=True)
        deadline = time.monotonic() + 15
        while True:
            try:
                require(http.read("/healthz/ready")["status"] == "ready", "web is degraded")
                break
            except SmokeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield http, token


def run(release, work):
    finance_source = ROOT / "finance"
    finance_python = release / "runtime/venv/bin/python"
    data_root, research_root = work / "replay", work / "research"
    for directory in (data_root, research_root):
        directory.mkdir()
    target = os.environ.get("CARGO_TARGET_DIR") or work / "target"
    owner = build_owner(finance_source, work, target)
    snapshot = publish_strategy_replay(finance_source, finance_python, data_root, research_root)
    worker_env = {
        **local_env(),
        "FERTILE_DATA_ROOT": str(data_root),
        "FERTILE_RESEARCH_ROOT": str(research_root),
        "FINANCE_STRATEGY_BIN": str(release / "bin/finance-strategy"),
        "PYTHONPATH": str(release / "finance/python"),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "TMPDIR": str(work),
    }
    worker = [finance_python, "-B", "-m", "fertile_finance.worker", "--isolated", "--timeout", "45"]
    with (postgres(work) as database,
          running_owner(owner, work, data_root, research_root) as finance_origin,
          gateway(release, work, database, finance_origin) as (http, token),
          child(worker, work, worker_env, "worker", lambda log: b"finance-backtest-worker ready\n" in log)):
        exercise_strategy(http, token, finance_source, snapshot)
        check_market(http, token, finance_origin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    release = parser.parse_args().release.resolve(strict=True)
    verify_release(release)
    with tempfile.TemporaryDirectory(prefix="fertile-t3b.", dir="/tmp") as directory:
        run(release, Path(directory))


if __name__ == "__main__":
    main()
