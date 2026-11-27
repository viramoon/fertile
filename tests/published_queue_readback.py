"""Exercise two independently built finance API binaries against a synthetic queue."""

import argparse
from contextlib import closing, contextmanager
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time


REPO = Path(__file__).resolve().parents[1]
PUBLISHED_FERTILE = "699fe314342d6b6a99a48044f865a76e6587bade"
QUEUE = "finance-state/backtests/jobs.sqlite3"
NOTE = "synthetic-expand-v2"
JOBS = "/api/v1/backtests"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def build_api(source, revision, work, target):
    checkout = work / revision
    checkout.mkdir()
    with subprocess.Popen(["git", "-C", source, "archive", revision], stdout=subprocess.PIPE) as archive:
        # Fresh source mtimes prevent Cargo from reusing the other revision's
        # older workspace artifacts when the caller supplies a shared target.
        subprocess.run(["tar", "-xm", "-C", checkout], stdin=archive.stdout, check=True)
        require(archive.wait() == 0, "failed to archive the pinned finance source")
    subprocess.run(
        ["cargo", "build", "--offline", "--locked", "--release", "-p", "finance-api",
         "--bin", "finance-api", "--target-dir", target],
        cwd=checkout, env={**os.environ, "CARGO_NET_OFFLINE": "true"}, check=True,
    )
    # Preserve both outputs when Cargo reuses the same target directory.
    return Path(shutil.copy2(target / "release/finance-api", checkout / "finance-api"))


def installed_candidate(candidate, revision):
    manifest = candidate.parents[1] / "manifest.json"
    components = json.loads(manifest.read_text())["components"]
    require(candidate.name == "finance-api" and components["finance"] == revision,
            "candidate binary must come from the release pinned by this checkout")
    return candidate


def ephemeral_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


@contextmanager
def running_api(binary, work, data, research, config, phase):
    port = ephemeral_port()
    # JSON is valid YAML. Every path in the application config belongs to this private fixture.
    (config / "base.yaml").write_text(json.dumps({
        "api": {"bind_host": "127.0.0.1", "port": port},
        "paths": {"data_root": str(data), "research_root": str(research)},
    }), encoding="utf-8")
    ready_line = f"finance-api listening on 127.0.0.1:{port}".encode()
    log = work / f"{phase}.stderr"
    env = {
        "PATH": os.defpath, "LANG": "C", "FERTILE_PROFILE": "stable",
        "FINANCE_CONFIG_DIR": str(config),
    }
    with log.open("wb") as stderr:
        process = subprocess.Popen([str(binary)], cwd=work, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                # Use the actual child's bind acknowledgement before sending
                # even a health probe to the reserved loopback port.
                if ready_line in log.read_bytes().splitlines():
                    break
                time.sleep(0.025)
            else:
                raise RuntimeError(f"{phase} finance API did not bind its loopback port")
            request(port, "/healthz/live")
            request(port, "/healthz/ready")
            yield port
        finally:
            running = process.poll() is None
            if running:
                process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError(f"{phase} finance API did not terminate gracefully") from None
        require(running and process.returncode == 0, f"{phase} finance API exited unexpectedly")


def request(port, path, *, body=None, key=None):
    if body is None:
        method, status, headers = "GET", 200, {}
    else:
        method, status = "POST", 202
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        body = json.dumps(body).encode()
    with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        require(response.status == status, f"{method} {path} returned HTTP {response.status}, expected {status}")
        return json.loads(response.read(65_536))


def enqueue(port, key, minute):
    fixture = {
        "strategy": "DemoStrategy", "instrument": "6EH4.XCME", "params": {},
        "start": f"2024-01-01T23:{minute:02d}:00Z", "end": f"2024-01-01T23:{minute + 4:02d}:00Z",
    }
    job_id = request(port, JOBS, body=fixture, key=key)["id"]
    require(type(job_id) is int and job_id > 0, "finance API did not return a positive job ID")
    job = request(port, f"{JOBS}/{job_id}")
    require(job["request"] == fixture and job["status"] == "queued", "queued job differs from the synthetic request")
    return job


def expanded_rows(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        version, = conn.execute("PRAGMA user_version").fetchone()
        require(version == 2, "finance API changed the expanded fixture schema version")
        return conn.execute("SELECT id, idem, upgrade_note FROM jobs ORDER BY id").fetchall()


def rehearse(published, candidate, work):
    data, research, config = (work / name for name in ("data", "research", "config"))
    for path in (data, research, config):
        path.mkdir()
    (config / "stable.yaml").write_text("{}\n", encoding="utf-8")
    with running_api(published, work, data, research, config, "old-before") as port:
        before = enqueue(port, "t2a-old-writer", 2)

    database = data / QUEUE
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as conn, conn:
        conn.execute("ALTER TABLE jobs ADD COLUMN upgrade_note TEXT NOT NULL DEFAULT 'synthetic-expand-v2'")
        conn.execute("PRAGMA user_version = 2")

    with running_api(candidate, work, data, research, config, "candidate") as port:
        require(request(port, f"{JOBS}/{before['id']}") == before, "candidate could not read the pre-upgrade job")
        after = enqueue(port, "t2a-candidate-writer", 3)
        require(after["id"] > before["id"], "candidate did not append a new job")

    expected = [(before["id"], "t2a-old-writer", NOTE), (after["id"], "t2a-candidate-writer", NOTE)]
    require(expanded_rows(database) == expected, "candidate did not write to the shared expanded queue")
    with running_api(published, work, data, research, config, "old-after") as port:
        require(request(port, f"{JOBS}/{after['id']}") == after, "published binary could not read the candidate job")
        require(request(port, JOBS)["items"] == [before, after], "published binary could not list both jobs")
    require(expanded_rows(database) == expected, "published binary modified the upgraded storage")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def finance_gitlink(fertile_ref):
    return subprocess.check_output(
        ["git", "-C", REPO, "rev-parse", f"{fertile_ref}:finance"], text=True,
    ).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finance-source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, help="finance-api from the candidate's built fertile release")
    parser.add_argument("--build-only", action="store_true", help="build/verify both binaries without opening sockets")
    args = parser.parse_args()
    source = args.finance_source.resolve(strict=True)
    pin, old_pin = finance_gitlink("HEAD"), finance_gitlink(PUBLISHED_FERTILE)
    require(pin != old_pin, "finance candidate must differ from the published baseline")
    subprocess.run(["git", "-C", source, "merge-base", "--is-ancestor", old_pin, pin], check=True)
    with tempfile.TemporaryDirectory(prefix="fertile-published-queue.", dir="/tmp") as directory:
        work = Path(directory)
        target = Path(os.environ.get("CARGO_TARGET_DIR", work / "target")).resolve() / "published-queue"
        if args.candidate:
            release = installed_candidate(args.candidate.resolve(strict=True), pin)
            candidate = Path(shutil.copy2(release, work / "candidate-api"))
        else:
            candidate = build_api(source, pin, work, target)
        published = build_api(source, old_pin, work, target)
        require(digest(published) != digest(candidate), "published and candidate binaries are identical")
        if args.build_only:
            print("Built and verified distinct published and candidate binaries")
        else:
            rehearse(published, candidate, work)
            print("Published finance API read both jobs from the expanded queue written by the candidate")


if __name__ == "__main__":
    main()
