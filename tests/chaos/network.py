"""Verify bounded local paper-socket loss and optionally check a configured HTTPS origin."""
import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import supervisor_drill as drill
import healthwatch
import e2e_smoke

ROOT = drill.ROOT
NS_MINUTE = 60_000_000_000
MARKET_KEYS = {"market:fresh", "service:finance-collector:ready"}


def require(value, message):
    if not value:
        raise RuntimeError(message)


def session(release, opened, closed, duration):
    now = datetime.now(timezone.utc)
    bounds = json.loads(subprocess.check_output(
        [release / "runtime/venv/bin/python", HERE / "network-session.py", now.isoformat()], timeout=30))
    require(bounds, "US equity exchange is closed today")
    start, end = map(datetime.fromisoformat, bounds)
    require(bool(opened) == bool(closed), "supply both session bounds to narrow the official schedule")
    if opened:
        a, b = map(datetime.fromisoformat, (opened, closed))
        require(a.tzinfo and b.tzinfo and start <= a < b <= end,
                "operator window must lie inside the exchange session, including early closes")
        start, end = a, b
    require(start + timedelta(minutes=2) < now < end - timedelta(seconds=duration + 360),
            "outside confirmed session or too close to closing time")
    return start


def public_target(args, plan):
    if args.protocol_fixture:
        require(not args.public_origin, "protocol fixture must never use a public domain")
        if args.fixture_origin:
            u = urlsplit(args.fixture_origin)
            require(u.scheme == "https" and u.hostname == "127.0.0.1" and u.port and
                    not u.username and not u.password and u.path in ("", "/") and not u.query and not u.fragment,
                    "fixture origin must be an explicit https://127.0.0.1:<port>")
        require(configured_origin(plan) == args.fixture_origin, "healthwatch and the fixture origin must agree")
        return args.fixture_origin
    require(not args.fixture_origin, "fixture origin only works in protocol fixture mode")
    if args.paper_only:
        require(not args.public_origin, "paper-only mode does not take a public origin")
    if not args.public_origin:
        return None
    configured = configured_origin(plan)
    message = "--public-origin must match the HTTPS origin explicitly configured for this deployment's healthwatch"
    require(isinstance(configured, str) and configured, message)
    try:
        configured = e2e_smoke.public_origin(configured)
    except argparse.ArgumentTypeError:
        raise RuntimeError(message) from None
    require(e2e_smoke.origin_key(urlsplit(args.public_origin)) == e2e_smoke.origin_key(urlsplit(configured)), message)
    return args.public_origin


def configured_origin(plan):
    env = plan["services"]["healthwatch"]["env"]
    if env.get("FERTILE_PUBLIC_ORIGIN"):
        return env["FERTILE_PUBLIC_ORIGIN"]
    if path := env.get("FERTILE_HEALTH_CONFIG"):
        conf = yaml.safe_load(Path(path).read_text()) or {}
        return conf.get("public_origin")
    return None


def marks(path):
    result = json.loads(path.read_bytes())
    require(isinstance(result, dict) and result and all(isinstance(k, str) and type(v) is int and
            v > 0 and v % NS_MINUTE == 0 for k, v in result.items()), "invalid durable minute watermarks")
    return result


def catalog_window(release, catalog, start, end):
    proc = subprocess.run([release / "runtime/venv/bin/python", HERE / "network-catalog.py", catalog],
                          input=json.dumps(dict(start=start, end=end)), text=True, stdout=subprocess.PIPE,
                          check=True, timeout=40)
    return json.loads(proc.stdout)


def local_market(api):
    code, body = healthwatch.http_json(api, 3, local=True)
    if code != 200 or not isinstance(body, dict) or body.get("source") != "ib-realtime":
        return None
    rows = body.get("instruments")
    if body.get("state") != "ready" or not rows or any(r.get("stale") is not False for r in rows):
        return None
    return {row["instrument"] for row in rows}


def paper_probe(release, fixture):
    if fixture:
        env = dict(os.environ, FINANCE_CONFIG_DIR=str(release / "finance/config"), IB_GATEWAY_ADDRESS="127.0.0.1:4002")
        out = subprocess.check_output([release / "bin/finance-ib", "probe"], env=env, stderr=subprocess.DEVNULL, timeout=15)
        report = json.loads(out)
        accounts = report["paper_accounts"]
        require(accounts and all(a.startswith("DU") for a in accounts), "fixture paper prefix mismatch")
    else:
        report = json.loads(subprocess.check_output([drill.CLI.with_name("ib-check.sh")], timeout=20))
        require(report.get("state") == "ready" and report.get("account_prefix") == "DU",
                "paper Gateway authentication or identity failed")
    require(abs(report["server_time_unix"] - time.time()) < 120, "Gateway server time is not current")
    drill.report("paper_protocol_probe", fixture=fixture, server_time_unix=report["server_time_unix"])


def tcp():
    try:
        with socket.create_connection(("127.0.0.1", 4002), timeout=1):
            return True
    except OSError:
        return False


@contextmanager
def blocked_port(duration):
    helper = str(HERE / "network-filter.sh")
    direct = subprocess.run(["iptables", "-w", "4", "-S", "OUTPUT"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=8)
    privilege = "direct" if direct.returncode == 0 else "sudo"
    label = "fertile-drill-" + uuid4().hex
    # The helper holds privileges for cleanup and notices EOF even when this
    # coordinator receives SIGKILL. Never flush a table or alter a global policy.
    with subprocess.Popen([helper, privilege, str(duration), label], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, start_new_session=True) as guard:
        try:
            require(select.select([guard.stdout], [], [], 12)[0], "timeout installing the paper rule")
            ack = guard.stdout.readline().split()
            require(len(ack) == 2 and ack[0] == b"active", "could not install the scoped paper rule")
            yield guard, float(ack[1])
        finally:
            guard.stdin.close()
            require(guard.wait(timeout=15) == 0, f"paper netfilter helper failed; inspect rule {label}")


def transitions(path, offset):
    result = {}
    with path.open("rb") as stream:
        stream.seek(offset)
        for line in stream:
            if not line.endswith(b"\n"):
                continue
            item = json.loads(line)
            if item["check"] in {*MARKET_KEYS, "public:ready"}:
                result.setdefault(item["check"], []).append(item)
    return result


def completed(alerts, key, start, cutoff):
    # Accept only this fault's transition and its subsequent clearing.
    failed_at = None
    for entry in alerts.get(key, []):
        at = datetime.fromisoformat(entry["timestamp"]).timestamp()
        if entry["event"] == "failure" and start <= at <= cutoff:
            failed_at = at
        if entry["event"] == "recovery" and failed_at is not None and at > failed_at and at >= cutoff - .1:
            return True
    return False


def run(args):
    require(120 <= args.duration <= 300, "duration must be 120..300 seconds")
    root = ROOT
    plan = json.loads((root / "run/services.json").read_bytes())
    release = (root / "current").resolve(strict=True)
    require(release == Path(plan["release"]).resolve(strict=True), "supervisor plan and active release differ")
    origin = public_target(args, plan)
    opened = None if args.protocol_fixture else session(release, args.session_open, args.session_close, args.duration)
    daemon, active, skipped = drill.running_initial()
    required = {"finance-api", "finance-collector", "web", "healthwatch", "postgres", "finance-backtest-worker"}
    if not args.protocol_fixture:
        required.add("ib-gateway")
    require(not healthwatch.webhook_url(root), "use a maintenance window with local alert delivery only")
    require(required <= active.keys(), "required services missing: " + str(sorted(required - active.keys())))
    if args.protocol_fixture:
        # Keep a loopback TLS fixture on loopback even if the calling shell sets a proxy.
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,::1,localhost"
        e2e_smoke.require_fixture_environment(root, Path(os.environ.get("FERTILE_BOX_HOME", "/home/box")))
        require(all(skipped.get(n) == "disabled" for n in ("ib-gateway", "xvfb")),
                "fixture requires disabled real Gateway and display")
    require(all(not active[n].get("ready_url") or active[n]["ready"] == 200 for n in required),
            "required services must initially be ready")
    coll = plan["services"]["finance-collector"]["env"]
    data = Path(coll["FERTILE_DATA_ROOT"])
    config = yaml.safe_load(Path(coll["FINANCE_COLLECTOR_CONFIG"]).read_bytes())
    require(config["source"] == "ib" and config["bar_spec"] == "1-MINUTE-LAST" and
            coll.get("IB_GATEWAY_ADDRESS", "127.0.0.1:4002") == "127.0.0.1:4002", "requires local paper and one-minute real-time TRADES")
    catalog = data / config["catalog"]
    state = data / "finance-state/collector"
    marker = json.loads((catalog / ".finance-collector/source.json").read_bytes())
    require(marker == json.loads((state / "catalog.json").read_bytes()) and marker["source"] == "ib",
            "catalog provenance is not this collector's IB source")
    api = active["finance-api"]["ready_url"].split("/healthz/")[0] + "/api/v1/market/status"
    ready_url = active["finance-collector"]["ready_url"]
    start = marks(state / "watermarks.json")
    require(set(config["instruments"]) == set(start) == local_market(api), "instruments or live market state differ")
    require(all(0 <= time.time_ns() - ns < config["max_age_seconds"] * 1_000_000_000 for ns in start.values()),
            "on-disk baseline watermarks are not fresh")
    if opened:
        require(min(start.values()) >= int(opened.timestamp()) * 1_000_000_000, "watermark predates the session")
    before_bars = catalog_window(release, catalog, start, start)
    if origin:
        require(healthwatch.public_ready(origin, 4)["state"] == "ok", "origin not ready before outage")
    if origin and not args.protocol_fixture:
        suffix = "/api/v1/system/status"
        local = active["web"]["ready_url"].split("/healthz/")[0] + suffix
        left, local_status = healthwatch.http_json(local, 3, local=True)
        right, external_status = healthwatch.http_json(origin + suffix, 5)
        require(left == right == 200 and isinstance(local_status, dict) and isinstance(external_status, dict)
                and isinstance(local_status.get("web"), dict) and local_status["web"].get("started_at")
                and local_status["web"] == external_status.get("web"),
                "confirmed origin and local web do not serve the same process")
    alerts = root / "logs/alerts/alerts.jsonl"
    cursor = alerts.stat().st_size
    paper_probe(release, args.protocol_fixture)
    mode = "protocol-fixture" if args.protocol_fixture else "full" if origin else "paper-only"
    omitted = (["real IB account", "exchange session", "real Cloudflare Tunnel", "public smoke"]
               if args.protocol_fixture else ["Ruler-managed tunnel recovery", "public smoke"] if not origin
               else ["Ruler-managed tunnel recovery"])
    drill.report("network_baseline", mode=mode, duration_seconds=args.duration, bars=before_bars,
                 unverified=omitted, skipped_services=skipped)
    with ExitStack() as stack:
        stack.callback(os.close, drill.identity(daemon, daemon=True))
        fault_wall = time.time()
        with blocked_port(args.duration) as active_filter:
            guard, filter_start = active_filter
            drill.report("network_fault_active", mode=mode)
            failed_origin = False
            degraded_collector = False
            require(not tcp(), "4002 stayed reachable during scoped DROP")
            while guard.poll() is None:
                if not degraded_collector:
                    degraded_collector = healthwatch.http_json(ready_url, 2, local=True)[0] == 503
                if args.protocol_fixture and origin and not failed_origin:
                    failed_origin = healthwatch.public_ready(origin, 2)["state"] == "failed"
                time.sleep(.25)
            cleared = guard.stdout.readline().split()
            require(len(cleared) == 3 and cleared[0] == b"cleared", "no confirmed removal of the paper rule")
            elapsed = float(cleared[1]) - filter_start
            cutoff_wall = float(cleared[2])
        ended = time.monotonic()
        released_ns = time.time_ns()
        drill.report("network_fault_cleared", duration_seconds=round(elapsed, 2))
    require(elapsed >= args.duration - .2, "fault stopped before requested duration")
    local_tls_fault = args.protocol_fixture and origin is not None
    require(degraded_collector and (not local_tls_fault or failed_origin),
            "missing observed collector or fixture origin failure")
    require(tcp(), "paper TCP did not reopen")
    paper_probe(release, args.protocol_fixture)
    target = (released_ns // NS_MINUTE + 1) * NS_MINUTE
    deadline = ended + 300
    recovery = {}
    while time.monotonic() < deadline:
        end = marks(state / "watermarks.json")
        events = transitions(alerts, cursor)
        market_recovered = any(completed(events, key, fault_wall, cutoff_wall) for key in MARKET_KEYS)
        market_ok = set(start) == set(end) == local_market(api) and healthwatch.http_json(ready_url, 2, local=True)[0] == 200
        origin_ok = not origin or healthwatch.public_ready(origin, 3)["state"] == "ok"
        elapsed = round(time.monotonic() - ended, 2)
        if market_ok:
            recovery.setdefault("collector_ready_seconds", elapsed)
        if local_tls_fault and origin_ok:
            recovery.setdefault("fixture_origin_ready_seconds", elapsed)
        if market_ok and min(end.values()) >= target:
            recovery.setdefault("post_outage_watermark_seconds", elapsed)
        if (market_ok and min(end.values()) >= target and market_recovered and origin_ok
                and (not local_tls_fault or completed(events, "public:ready", fault_wall, cutoff_wall))):
            break
        time.sleep(1)
    else:
        raise TimeoutError("no verified market recovery or origin readiness, post-outage watermark or matching alert transitions: "
                           + str({k: [r["event"] for r in v] for k, v in events.items()}))
    final_status = drill.status()
    require(final_status["supervisor_pid"] == daemon and
            final_status["services"]["finance-collector"]["pid"] == active["finance-collector"]["pid"],
            "collector or supervisor restarted during network fault; in-process reconnect was not verified")
    bars = catalog_window(release, catalog, start, end)
    drill.report("network_recovery_verified", recovery_seconds=round(time.monotonic() - ended, 2), stages=recovery, bars=bars,
                 alerts={k: [(item["event"], item["timestamp"]) for item in records] for k, records in events.items()})
    smoke = HERE.parent / "e2e-smoke.sh"
    subprocess.run([smoke, "local", *(["--protocol-fixture"] if args.protocol_fixture else [])], check=True, timeout=260)
    if origin and not args.protocol_fixture:
        subprocess.run([smoke, "public", origin], check=True, timeout=260)
    drill.report("network_drill_complete", mode=mode, unverified=omitted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--paper-only", action="store_true", help="require local-only checks (the default)")
    modes.add_argument("--protocol-fixture", action="store_true")
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--public-origin", type=e2e_smoke.public_origin,
                        help="also check the HTTPS origin explicitly configured for healthwatch")
    parser.add_argument("--fixture-origin")
    parser.add_argument("--session-open")
    parser.add_argument("--session-close")
    args = parser.parse_args()
    def stopped(signum, frame):
        raise KeyboardInterrupt
    for number in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(number, stopped)
    with (ROOT / "run/network-drill.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args)
