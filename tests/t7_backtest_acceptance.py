"""Exercise synthetic backtest submission and anonymous pagination in disposable state."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import subprocess
import tempfile

from e2e_smoke import HTTP, SmokeError, log, require
from published_queue_readback import archive_finance, ephemeral_port
from t3b_integration import COMPONENTS, ROOT, child, gateway, isolated_strategy_worker, local_env, postgres
from t3b_strategy import (
    BACKTESTS, FIXTURE, post, publish_strategy_replay, read_strategy_result,
    select_uploaded_version, submit,
)


PRIVATE_GAP = 110  # Larger than the maximum upstream page size of 100.
PRIVATE_TAIL = 8
SUBMISSION_KEY = "t7-selected-strategy"
LEGACY = {
    "strategy": "SmaCrossover", "instrument": "FIXTURE.XNYS",
    "start": "2024-11-01T00:00:00Z", "end": "2024-12-20T00:00:00Z",
    "params": {"fast_period": 5, "slow_period": 20, "shares": 10},
}


def verify_candidate(release):
    """Allow a T3b candidate or a new build with the checkout's exact component pins."""
    def revision(repo, ref):
        return subprocess.check_output(
            ["git", "-C", repo, "rev-parse", ref], env=local_env(ROOT), text=True,
        ).strip()

    pins = {name: revision(ROOT, f"HEAD:{name}") for name in COMPONENTS}
    manifest = json.loads((release / "manifest.json").read_bytes())
    require(manifest.get("components") == pins, "candidate component revisions differ from this checkout's gitlinks")
    require(revision(ROOT / "finance", "HEAD") == pins["finance"],
            "finance fixture checkout differs from the committed gitlink")
    return pins["finance"]


def isolated_port():
    try:
        port = ephemeral_port()
    except OSError as error:
        raise SmokeError(f"could not allocate an IPv4 loopback port: {error}") from None
    require(port >= 1024 and port not in {4001, 4002, 5432, 7496, 7497, 8080, 8081, 8082, 8090},
            "kernel selected a reserved port for acceptance")
    return port


@contextmanager
def finance_api(release, work, data_root, research_root):
    port = isolated_port()
    address = f"127.0.0.1:{port}"
    command = [release / "bin/finance-api", "serve-isolated", data_root, research_root, str(port)]
    with child(command, work, {**local_env(work), "TOKIO_WORKER_THREADS": "2"}, "finance-api",
               lambda output: address.encode() in output.splitlines()):
        http = HTTP(f"http://{address}", local=True)
        http.read("/healthz/ready")
        yield http


def check_admin_queue(http, headers, jobs):
    """Prove every fixture job is present through the same gateway."""
    cursor = 0
    for start in range(0, len(jobs), 100):
        expected = jobs[start:start + 100]
        page = http.read(f"{BACKTESTS}?limit=100&cursor={cursor}", headers=headers)
        require([(job["id"], job["request"]["strategy"]) for job in page["items"]] == expected,
                "admin page did not match the interleaved fixture queue")
        cursor = page["next_cursor"]
        require(cursor == (expected[-1][0] if start + len(expected) < len(jobs) else None),
                "admin page did not report the expected continuation")


def check_anonymous_pagination(http, public_ids, private_ids):
    cursor = None
    for index, expected_id in enumerate(public_ids):
        query = "limit=1" if cursor is None else f"limit=1&cursor={cursor}"
        page = http.read(f"{BACKTESTS}?{query}")
        require([(job["id"], job["request"]["strategy"]) for job in page["items"]]
                == [(expected_id, "SmaCrossover")], "anonymous page missed a public job or returned a private job")
        cursor = page["next_cursor"]
        if index + 1 == len(public_ids):
            require(cursor is None, "private tail produced an anonymous next cursor")
        else:
            require(type(cursor) is int and cursor == expected_id and cursor not in private_ids,
                    "anonymous cursor did not identify the public job just returned")

    require(http.read(f"{BACKTESTS}?limit=1&cursor={public_ids[-1]}") == {"items": [], "next_cursor": None},
            "anonymous list exposed a job or cursor after the last public job")
    for job_id in (private_ids[0], private_ids[PRIVATE_GAP - 1], private_ids[-1]):
        response = http.read(f"{BACKTESTS}/{job_id}", expected=404)
        require(response["error"]["code"] == "job_not_found", "anonymous detail exposed a private job")
    log("T7 anonymous pagination passed", public_jobs=len(public_ids), private_jobs=len(private_ids),
        gap=PRIVATE_GAP, tail=PRIVATE_TAIL, limit=1)


def exercise_backtests(release, work, data_root, research_root, source, upstream, http, token):
    headers = {"Authorization": f"Bearer {token}"}
    selected = select_uploaded_version(http, headers, (source / FIXTURE).read_text(encoding="utf-8"))
    version = selected["strategy"]
    request = {
        "strategy": "StrategyBundle", "start": "2024-03-01T00:00:00Z", "end": "2024-04-15T00:00:00Z",
        "source": {"kind": "registered", "id": version["id"], "version": version["version"]},
    }
    missing = post(http, BACKTESTS, request, {**headers, "Idempotency-Key": SUBMISSION_KEY}, expected=422)
    require(missing["error"]["code"] == "no_data", "empty data root did not reject the missing replay")
    require(http.read(BACKTESTS, headers=headers) == {"items": [], "next_cursor": None},
            "missing replay left a job or the acceptance queue was already populated")

    python = release / "runtime/venv/bin/python"
    snapshot = publish_strategy_replay(source, python, data_root, research_root)
    first = submit(http, headers, "t7-public-first", LEGACY)
    selected_id = submit(http, headers, SUBMISSION_KEY, request)
    require(http.read(BACKTESTS + "/by-key", headers={**headers, "Idempotency-Key": SUBMISSION_KEY})
            == {"id": selected_id}, "backtest key did not resolve to the selected strategy job")
    with isolated_strategy_worker(release, work, data_root, research_root):
        read_strategy_result(http, headers, selected_id, {**request, "content_sha256": version["content_sha256"]},
                             selected["plan"], snapshot)
    log("T7 isolated replay submission and result readback passed")

    # Seed the large gap through the isolated upstream's normal admission path.
    # The worker is stopped so the seed jobs do not consume engine time.
    gap = [selected_id] + [submit(upstream, {}, f"t7-gap-{index}", request)
                           for index in range(PRIVATE_GAP - 1)]
    second = submit(http, headers, "t7-public-second", {**LEGACY, "end": "2024-12-19T00:00:00Z"})
    third = submit(http, headers, "t7-public-third", {**LEGACY, "end": "2024-12-18T00:00:00Z"})
    tail = [submit(upstream, {}, f"t7-tail-{index}", request) for index in range(PRIVATE_TAIL)]
    public_ids = [first, second, third]
    ordered = [first, *gap, second, third, *tail]
    require(ordered == sorted(set(ordered)), "public and private IDs did not interleave as intended")
    check_admin_queue(http, headers, [(job_id, "SmaCrossover" if job_id in public_ids else "StrategyBundle")
                                     for job_id in ordered])
    check_anonymous_pagination(http, public_ids, gap + tail)


def run(release, finance_pin):
    # Build every state path below a directory created exclusively for this invocation.
    with tempfile.TemporaryDirectory(prefix="fertile-t7.", dir="/tmp") as directory:
        work = Path(directory).resolve(strict=True)
        data_root, research_root = work / "replay", work / "research"
        data_root.mkdir()
        research_root.mkdir()
        source = archive_finance(ROOT / "finance", finance_pin, work, env=local_env(work))
        with (finance_api(release, work, data_root, research_root) as upstream,
              postgres(work) as database,
              gateway(release, work, database, upstream.origin, port=isolated_port()) as (http, token)):
            exercise_backtests(release, work, data_root, research_root, source, upstream, http, token)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--release", type=Path, required=True, help="trusted candidate artifact directory")
    release = parser.parse_args(argv).release.resolve(strict=True)
    run(release, verify_candidate(release))


if __name__ == "__main__":
    def terminate(_signum, _frame):
        raise SystemExit(128 + signal.SIGTERM)

    signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except (SmokeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
