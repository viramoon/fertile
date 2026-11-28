"""Check strategy registration, simulated backtests and job visibility through web."""

from decimal import Decimal
from hashlib import sha256
import json
import re
import subprocess
import time

from e2e_smoke import ROOT, log, require, summary


STRATEGIES = "/api/v1/strategies"
BACKTESTS = "/api/v1/backtests"
STRATEGY_ID = "example.equity-momentum"
FIXTURE_VERSION = "1.0.0"
NEWER_VERSION = "1.0.1"
FIXTURE = "strategies/fixtures/strategy-bundle/v1/entry-exit.yaml"
SNAPSHOT = ("finance-state/backtests/strategy-inputs/example.reviewed-us-daily@1.0.0/"
            "example.us-cash-equities@1.0.0.json")


def publish_strategy_replay(finance_source, finance_python, data_root, research_root):
    """Generate the configured replay and the legacy daily catalog in disposable roots."""
    env = {"PYTHONPATH": str(finance_source / "python"), "FERTILE_RESEARCH_ROOT": str(research_root),
           "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    subprocess.run(
        [finance_python, "-B", finance_source / "tests/config_strategy_replay_fixture.py", data_root],
        check=True, timeout=60, env=env,
    )
    subprocess.run([finance_python, "-B", ROOT / "tests/research-fixture.py"], check=True, timeout=60, env=env)
    return sha256((data_root / SNAPSHOT).read_bytes()).hexdigest()


def post(http, path, request, headers, expected=200):
    return http.read(path, data=json.dumps(request).encode(), expected=expected,
                     headers={**headers, "Content-Type": "application/json"})


def select_uploaded_version(http, headers, content):
    document = {"format": "yaml", "content": content}
    preview = post(http, STRATEGIES + "/validate", document, headers)
    registered = post(http, STRATEGIES, document, headers)
    require((registered["id"], registered["version"]) == (STRATEGY_ID, FIXTURE_VERSION),
            "web did not register the synthetic strategy version")
    version_field = f'version: "{FIXTURE_VERSION}"'
    require(content.count(version_field) == 1, "unexpected version field in the finance fixture")
    newer = post(http, STRATEGIES, {
        "format": "yaml", "content": content.replace(version_field, f'version: "{NEWER_VERSION}"'),
    }, headers)
    require((newer["id"], newer["version"]) == (STRATEGY_ID, NEWER_VERSION)
            and newer["content_sha256"] != registered["content_sha256"],
            "web did not register a separate newer strategy version")
    versions = http.read(f"{STRATEGIES}?id={STRATEGY_ID}", headers=headers)
    require(versions["items"] == [registered, newer], "web did not list both registered versions")

    selected = http.read(f"{STRATEGIES}/{STRATEGY_ID}/versions/{FIXTURE_VERSION}", headers=headers)
    digest = registered["content_sha256"]
    require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            and selected == preview and selected["strategy"] == registered
            and selected["plan"]["content_sha256"] == digest
            and selected["plan"]["source_sha256"] == sha256(content.encode()).hexdigest(),
            "selecting the uploaded version did not return its original compiled plan")
    return selected


def submit(http, headers, key, request):
    submitted = post(http, BACKTESTS, request, {**headers, "Idempotency-Key": key}, expected=202)
    job_id = submitted["id"]
    require(type(job_id) is int and job_id > 0, "backtest submission did not return a positive integer ID")
    return job_id


def read_strategy_result(http, headers, job_id, request, plan, snapshot_sha256):
    deadline = time.monotonic() + 90
    while True:
        job = http.read(f"{BACKTESTS}/{job_id}", headers=headers)
        require(job["id"] == job_id and job["request"] == request,
                "web job detail lost the selected strategy reference or content hash")
        phase = job["status"]
        if phase == "succeeded":
            break
        require(phase in {"queued", "running"}, f"strategy backtest entered {phase}: {job.get('error')}")
        require(time.monotonic() < deadline, "strategy backtest did not finish before the deadline")
        time.sleep(1)

    result = job["summary"]
    summary(result)
    require(result["strategy"] == "StrategyBundle" and result["instrument"] == "PORTFOLIO"
            and result["total_orders"] > 0 and job.get("error") is None,
            "strategy backtest did not return a successful simulated portfolio result")
    manifest = result["run_manifest"]
    require(manifest["bundle"] == plan["bundle"] and manifest["content_sha256"] == plan["content_sha256"]
            and manifest["data"]["snapshot_sha256"] == snapshot_sha256,
            "backtest result is not tied to the selected plan and generated replay bytes")
    report = result["strategy_report"]
    final_equity, initial_cash = report["equity"][report["pnl_as_of"]], report["initial_cash"]
    require(all(isinstance(value, str) for value in (final_equity, initial_cash))
            and Decimal(final_equity) - Decimal(initial_cash) == Decimal(result["pnl"]),
            "readback lost exact simulated cash or equity")
    require(any(fill["instrument_id"] == "BBB.XNYS" and fill["side"] == "buy"
                and fill["signal_date"] == "2024-03-01" and fill["fill_date"] == "2024-03-04"
                and isinstance(fill["price"], str) and Decimal(fill["price"]) == Decimal("162.08")
                for fill in report["fills"]),
            "missing expected exact-price fill from the synthetic replay")
    log("T3b strategy upload, version selection and result readback passed", id=job_id)


def check_anonymous_pagination(http, headers, first_id, private_id, second_id, trailing_id):
    ids = [first_id, private_id, second_id, trailing_id]
    require(ids == sorted(set(ids)), "public and restricted job IDs were not interleaved in the queue")
    administrator = http.read(f"{BACKTESTS}?limit=4", headers=headers)
    require([job["id"] for job in administrator["items"]] == ids
            and [job["request"]["strategy"] for job in administrator["items"]]
            == ["SmaCrossover", "StrategyBundle", "SmaCrossover", "StrategyBundle"]
            and administrator["next_cursor"] is None,
            "administrator list did not confirm both public and both restricted jobs")

    first = http.read(f"{BACKTESTS}?limit=1")
    require([job["id"] for job in first["items"]] == [first_id]
            and first["next_cursor"] == first_id and type(first["next_cursor"]) is int,
            "anonymous first page did not return the first public job and its cursor")
    second = http.read(f'{BACKTESTS}?limit=1&cursor={first["next_cursor"]}')
    require([job["id"] for job in second["items"]] == [second_id] and second["next_cursor"] is None,
            "anonymous continuation did not skip the restricted gap or end at the last public job")
    require(http.read(f"{BACKTESTS}?limit=1&cursor={second_id}") == {"items": [], "next_cursor": None},
            "anonymous list did not stay empty after the last public job")
    for job_id in (private_id, trailing_id):
        response = http.read(f"{BACKTESTS}/{job_id}", expected=404)
        require(response["error"]["code"] == "job_not_found", "anonymous detail exposed a restricted job")
    log("T3b anonymous backtest pagination passed", pages=2, public_jobs=2, restricted_jobs=2)


def exercise_strategy(http, admin_token, finance_source, snapshot_sha256):
    """Exercise the locked finance API, worker and web gateway with a fresh queue."""
    headers = {"Authorization": "Bearer " + admin_token}
    content = (finance_source / FIXTURE).read_text(encoding="utf-8")
    selected = select_uploaded_version(http, headers, content)
    version = selected["strategy"]
    request = {
        "strategy": "StrategyBundle", "start": "2024-03-01T00:00:00Z", "end": "2024-04-15T00:00:00Z",
        "source": {"kind": "registered", "id": version["id"], "version": version["version"]},
    }
    legacy = {
        "strategy": "SmaCrossover", "instrument": "FIXTURE.XNYS",
        "start": "2024-11-01T00:00:00Z", "end": "2024-12-20T00:00:00Z",
        "params": {"fast_period": 5, "slow_period": 20, "shares": 10},
    }
    first_id = submit(http, headers, "t3b-public-first", legacy)
    private_id = submit(http, headers, "t3b-strategy", request)
    second_id = submit(http, headers, "t3b-public-second", {**legacy, "end": "2024-12-19T00:00:00Z"})
    trailing_id = submit(http, headers, "t3b-strategy-trailing", request)
    require(http.read(BACKTESTS + "/by-key", headers={**headers, "Idempotency-Key": "t3b-strategy"})
            == {"id": private_id}, "backtest key did not resolve to the selected strategy job")

    check_anonymous_pagination(http, headers, first_id, private_id, second_id, trailing_id)
    read_strategy_result(http, headers, private_id, {**request, "content_sha256": version["content_sha256"]},
                         selected["plan"], snapshot_sha256)
