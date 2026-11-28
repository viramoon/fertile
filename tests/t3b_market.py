"""Read in-memory synthetic market data through the pinned finance and web routers."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from e2e_smoke import HTTP, require
from published_queue_readback import archive_finance


OWNER = "fertile_t3b_market_owner"
AS_OF = "2025-03-07T16:02:00Z"
PRICE = "123.4500000000000000000000000"
CHAIN = "/api/v1/options/chain?underlying_con_id=8&limit=1"
DEPTH = "/api/v1/market/depth?symbol=QQQ"
EXPOSURE = f"/api/v1/options/exposure?underlying_id=SYNTH.XNAS&as_of={AS_OF}&scenario=call_long_put_short"
FLOW = "/api/v1/options/flow"
FLOW_WINDOW = "con_id=42&start=2025-03-07T16:00:00Z&end=2025-03-07T16:00:12Z"
LOOPBACK_ORIGIN = re.compile(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}")


def build_owner(finance_source, work, target):
    """Build against a disposable archive of the committed producer, using its own Cargo.lock."""
    finance_source = Path(finance_source).resolve(strict=True)
    work = Path(work).resolve(strict=True)
    pin = subprocess.check_output(
        ["git", "-C", finance_source.parent, "rev-parse", "HEAD:finance"], text=True,
    ).strip()
    checkout = subprocess.check_output(
        ["git", "-C", finance_source, "rev-parse", "HEAD"], text=True,
    ).strip()
    require(checkout == pin, "synthetic market owner requires the committed finance gitlink")
    source = archive_finance(finance_source, pin, work)
    owner_path = source / "crates/finance-api/tests" / f"{OWNER}.rs"
    require(not owner_path.exists(), "synthetic market owner test name collides with finance")
    shutil.copyfile(Path(__file__).with_name("t3b_market_owner.rs"), owner_path)
    command = [
        "cargo", "test", "--offline", "--locked", "--release", "-p", "finance-api",
        "--test", OWNER, "--no-run", "--message-format=json-render-diagnostics",
        "--target-dir", str(Path(target).resolve()),
    ]
    binary = None
    with subprocess.Popen(command, cwd=source, stdout=subprocess.PIPE, text=True,
                          env={**os.environ, "CARGO_NET_OFFLINE": "true"}) as cargo:
        for line in cargo.stdout:
            message = json.loads(line)
            if message.get("reason") == "compiler-artifact" and message["target"]["name"] == OWNER:
                binary = message.get("executable")
        require(cargo.wait() == 0 and binary, "pinned synthetic market owner failed to build")
    return Path(shutil.copy2(binary, source / OWNER))


@contextmanager
def running_owner(owner, work, data_root, research_root):
    """Serve finance's real router on a dynamically allocated loopback socket."""
    work = Path(work).resolve(strict=True)
    env = {
        "PATH": os.defpath,
        "LANG": "C",
        "FERTILE_DATA_ROOT": str(Path(data_root).resolve(strict=True)),
        "FERTILE_RESEARCH_ROOT": str(Path(research_root).resolve(strict=True)),
    }
    for name in ("FERTILE_DATA_ROOT", "FERTILE_RESEARCH_ROOT"):
        require(Path(env[name]).is_relative_to(work), "synthetic owner requires isolated roots")
    log = work / "t3b-market-owner.stdout"
    with log.open("xb") as output:
        process = subprocess.Popen(
            [owner, "--exact", "serve_owner", "--ignored", "--nocapture"],
            cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=output,
        )
        try:
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                match = re.search(rb"^T3B_OWNER_LISTENING=(127\.0\.0\.1:[1-9][0-9]*)$",
                                  log.read_bytes(), flags=re.MULTILINE)
                if match:
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("synthetic market owner did not bind its loopback socket")
            yield f"http://{match[1].decode('ascii')}"
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError("synthetic market owner did not shut down") from None
        require(process.returncode == 0, "synthetic market owner exited unexpectedly")


def check_market(http, token, finance_origin):
    """Compare real web responses with the pinned finance router and check fixture outcomes."""
    require(http.local and LOOPBACK_ORIGIN.fullmatch(http.origin)
            and LOOPBACK_ORIGIN.fullmatch(finance_origin), "market test requires two loopback origins")
    upstream = HTTP(finance_origin, local=True)
    headers = {"Authorization": f"Bearer {token}"}
    for path in (CHAIN, DEPTH, EXPOSURE, f"{FLOW}/observations?{FLOW_WINDOW}"):
        http.read(path, expected=401)

    def forwarded(path, finance_path=None):
        want = upstream.read(finance_path or path)
        got = http.read(path, headers=headers)
        require(got == want, f"{path.split('?')[0]} did not preserve the finance response")
        return got

    def access(payload, product, license="fixture.synthetic"):
        require(payload["access"] == {
            "source": "ibkr", "product": product, "license": license,
            "display_live": True, "record_local": False,
            "derive_non_display": False, "backup_personal_drive": False,
        }, "synthetic fixture display license did not survive web")

    first = forwarded(CHAIN)
    access(first, "opra")
    require(first["as_of"] == AS_OF and first["discovery_complete"] is False
            and first["next_after_con_id"] == 20 and first["items"] == [{
                "con_id": 20, "symbol": "SYNTH", "primary_exchange": None,
                "expiry": "2025-03-07", "strike": PRICE, "right": "put", "multiplier": "125",
            }], "option-chain first page lost exact strike or stable cursor")
    second = forwarded(f"{CHAIN}&after_con_id={first['next_after_con_id']}")
    require([row["con_id"] for row in second["items"]] == [29]
            and second["next_after_con_id"] is None, "option-chain continuation lost the last page")
    print("PASS option_chain_through_web", flush=True)

    depth = forwarded(DEPTH, "/api/v1/market/book?symbol=QQQ")
    access(depth, "nasdaq-totalview")
    require(depth["symbol"] == "QQQ" and depth["venue"] == "NASDAQ" and depth["state"] == "receiving"
            and depth["permissions"] == {"equity_l1_confirmed": True, "totalview_confirmed": True,
                                         "eds_confirmed": True}
            and depth["requested_rows"] == 5 and depth["returned_rows"] == 1
            and depth["received_at"] == AS_OF and depth["market_data_type"] is None
            and depth["bids"] == [{"price": PRICE, "size": "1.2500"}] and depth["asks"] == [],
            "Nasdaq depth lost confirmed state, timestamps or exact price and size")
    print("PASS nasdaq_depth_through_web", flush=True)

    exposure = forwarded(EXPOSURE)
    access(exposure, "opra")
    report = exposure["report"]
    crossings = {"status": "crossings", "crossings": [
        {"lower_spot": "100.0", "upper_spot": "100.0"},
        {"lower_spot": "120.0", "upper_spot": "130.0"},
    ]}
    # Each contract has OI=1, multiplier=100 and gamma=0.02 at spot=100.
    totals = {"absolute": "400", "gex": "0"}
    require(report["model_version"] == "gex-v1" and report["as_of"] == AS_OF
            and report["underlying_id"] == "SYNTH.XNAS" and report["scenario"] == "call_long_put_short"
            and report["all"]["totals"] == report["zero_dte"]["totals"] == totals
            and report["all"]["flip"] == crossings and report["zero_dte"]["flip"] == crossings
            and report["later"]["flip"] == {"status": "unavailable", "reason": "no_eligible_contracts"},
            "option exposure lost scenario, exact GEX, gamma flip or 0DTE coverage")
    print("PASS option_exposure_through_web", flush=True)

    observed_path = f"{FLOW}/observations?{FLOW_WINDOW}&limit=1"
    observed = forwarded(observed_path)
    access(observed, "opra", "synthetic-opra")
    clue = observed["items"][0]["clue"]
    evidence = clue["observation"]["evidence"]
    require(clue["status"] == "pending_verification" and clue["generation"] == "18446744073709551615"
            and evidence["sampled_last_price"]["value"] == "1.000000000000000001"
            and evidence["delta_contracts"] == "9007199254740993" and observed["next_offset"] == 1,
            "observed flow lost its pending state or exact evidence")
    observed_tail = forwarded(f"{observed_path}&offset={observed['next_offset']}")
    require(observed_tail["items"][0]["clue"]["observation"]["kind"] == "quote_widening"
            and observed_tail["next_offset"] is None, "observed flow continuation lost the last clue")

    verified_path = f"{FLOW}/verification?{FLOW_WINDOW}&limit=2"
    verified = forwarded(verified_path)
    access(verified, "opra-history", "synthetic-opra-history")
    require(verified["trades"][1]["premium"] == "53.125000000000000053125"
            and verified["issues"][0]["reason"] == "pacing" and verified["durable_through"] is None
            and verified["next_offset"] == 2, "verified flow lost exact premium or coverage information")
    verified_tail = forwarded(f"{verified_path}&offset={verified['next_offset']}")
    require(verified_tail["trades"][0]["link"]["action"]["revision"] == "18446744073709551615"
            and verified_tail["next_offset"] is None, "verified flow continuation lost the exact revision")
    print("PASS option_flow_through_web", flush=True)
