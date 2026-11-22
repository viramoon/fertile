"""Verified, bounded fault on the selected deployment's paper socket and Tunnel."""
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
from zoneinfo import ZoneInfo

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import supervisor_drill as drill
import healthwatch
import e2e_smoke

ROOT = drill.ROOT
NS_MINUTE = 60_000_000_000
PUBLIC = {"https://www.viramoon.com", "https://www.viramoonstar.com"}
MARKET_KEYS = {"market:fresh", "service:finance-collector:ready"}


def require(value, message):
    if not value:
        raise RuntimeError(message)


def session(opened, closed, duration):
    require(opened and closed, "supply the exchange's confirmed --session-open and --session-close")
    start, end = map(datetime.fromisoformat, (opened, closed))
    require(start.tzinfo and end.tzinfo, "session bounds must include timezone offsets")
    now = datetime.now(timezone.utc)
    eastern = ZoneInfo("America/New_York")
    a, b = start.astimezone(eastern), end.astimezone(eastern)
    require(a.date() == b.date() and a.weekday() < 5 and
            (a.hour, a.minute) >= (9, 30) and (b.hour, b.minute) <= (16, 0),
            "expected one regular US equity session (include holidays and early closes)")
    require(start + timedelta(minutes=2) < now < end - timedelta(seconds=duration + 360),
            "outside confirmed session or too close to closing time")
    return start


def public_target(args, configured):
    if args.protocol_fixture:
        require(not args.public_origin, "protocol fixture must never use a public domain")
        if args.fixture_origin:
            u = urlsplit(args.fixture_origin)
            require(u.scheme == "https" and u.hostname == "127.0.0.1" and u.port and
                    not u.username and not u.password and u.path in ("", "/") and not u.query and not u.fragment,
                    "fixture origin must be an explicit https://127.0.0.1:<port>")
        require(configured == args.fixture_origin, "healthwatch and the fixture origin must agree")
        return args.fixture_origin
    require(not args.fixture_origin, "fixture origin only works in protocol fixture mode")
    if args.paper_only:
        require(not args.public_origin, "paper-only mode does not accept a public origin")
        return None
    require(args.public_origin in PUBLIC and args.public_origin == configured,
            "explicitly confirmed public origin must match this deployment's healthwatch configuration")
    return args.public_origin


def configured_origin(plan, release):
    env = plan["services"]["healthwatch"]["env"]
    if env.get("FERTILE_PUBLIC_ORIGIN"):
        return env["FERTILE_PUBLIC_ORIGIN"]
    if path := env.get("FERTILE_HEALTH_CONFIG"):
        conf = yaml.safe_load(Path(path).read_text()) or {}
        if "public_origin" in conf:
            return conf["public_origin"]
    manifest = yaml.safe_load((release / "fertile/deploy/service.yaml").read_bytes())
    return manifest["infrastructure"]["tunnel"]["public_origin"]


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


def tunnel_fd(stack, daemon, row, plan):
    pid = row["pid"]
    fd = drill.identity(pid, parent=daemon)
    stack.callback(os.close, fd)
    argv = [os.fsdecode(a) for a in Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    configured = plan["services"]["cloudflared"]["command"]
    require(Path(argv[0]).name == "cloudflared" and Path(f"/proc/{pid}/exe").resolve().name == "cloudflared" and
            argv[1:] == configured[1:] and argv[1:2] == ["tunnel"] and argv[-3:] == ["--protocol", "quic", "run"],
            "PID does not match this deployment's supervised QUIC Tunnel")
    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    require(state not in ("T", "t", "Z"), "Tunnel already stopped or traced before drill")
    return fd


@contextmanager
def pause_tunnel(pid, fd, duration):
    if fd is None:
        yield
        return
    read_ctl, write_ctl = os.pipe()
    ack_r, ack_w = os.pipe()
    guard = os.fork()
    if guard == 0:
        os.close(write_ctl)
        os.close(ack_r)
        try:
            os.setsid()
            try:
                if not drill.freeze(pid, fd):
                    raise RuntimeError("Tunnel vanished before SIGSTOP")
                os.write(ack_w, b"1")
                select.select([read_ctl], [], [], duration + 5)
            finally:
                signal.pidfd_send_signal(fd, signal.SIGCONT)
        finally:
            os._exit(0)
    os.close(read_ctl)
    os.close(ack_w)
    try:
        require(select.select([ack_r], [], [], 5)[0] and os.read(ack_r, 1) == b"1", "Tunnel freeze failed")
        yield
    finally:
        os.close(write_ctl)
        os.close(ack_r)
        try:
            signal.pidfd_send_signal(fd, signal.SIGCONT)
        finally:
            os.waitpid(guard, 0)


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
        if entry["event"] == "recovery" and failed_at is not None and at > failed_at:
            return True
    return False


def run(args):
    require(120 <= args.duration <= 300, "duration must be 120..300 seconds")
    root = ROOT
    plan = json.loads((root / "run/services.json").read_bytes())
    release = (root / "current").resolve(strict=True)
    require(release == Path(plan["release"]).resolve(strict=True), "supervisor plan and active release differ")
    origin = public_target(args, configured_origin(plan, release))
    opened = None if args.protocol_fixture else session(args.session_open, args.session_close, args.duration)
    daemon, active, skipped = drill.running_initial()
    required = {"finance-api", "finance-collector", "web", "healthwatch", "postgres", "finance-backtest-worker"}
    if not args.protocol_fixture:
        required.add("ib-gateway")
    if not (args.paper_only or args.protocol_fixture):
        required.add("cloudflared")
    require(not healthwatch.webhook_url(root), "use a maintenance window with local alert delivery only")
    require(required <= active.keys(), "required services missing: " + str(sorted(required - active.keys())))
    if args.protocol_fixture:
        e2e_smoke.require_fixture_environment(root, Path(os.environ.get("FERTILE_BOX_HOME", "/home/box")))
        require(all(skipped.get(n) == "disabled" for n in ("ib-gateway", "xvfb", "cloudflared")),
                "fixture requires explicitly disabled real external services")
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
    if opened:
        require(min(start.values()) >= int(opened.timestamp()) * 1_000_000_000, "watermark predates the session")
    before_bars = catalog_window(release, catalog, start, start)
    if origin:
        require(healthwatch.public_ready(origin, 4)["state"] == "ok", "origin not ready before outage")
    alerts = root / "logs/alerts/alerts.jsonl"
    cursor = alerts.stat().st_size
    paper_probe(release, args.protocol_fixture)
    mode = "protocol-fixture" if args.protocol_fixture else "paper-only" if args.paper_only else "full"
    omitted = (["real IB account", "exchange session", "real Cloudflare Tunnel", "public smoke"]
               if args.protocol_fixture else ["Cloudflare Tunnel", "public smoke"] if args.paper_only else [])
    drill.report("network_baseline", mode=mode, duration_seconds=args.duration, bars=before_bars,
                 unverified=omitted, skipped_services=skipped)
    with ExitStack() as stack:
        stack.callback(os.close, drill.identity(daemon, daemon=True))
        tunnel = active.get("cloudflared", {}) if mode == "full" else {}
        fd = tunnel_fd(stack, daemon, tunnel, plan) if tunnel else None
        fault_wall = time.time()
        with pause_tunnel(tunnel.get("pid"), fd, args.duration), blocked_port(args.duration) as active_filter:
            guard, filter_start = active_filter
            drill.report("network_fault_active", mode=mode)
            failed_origin = False
            degraded_collector = False
            require(not tcp(), "4002 stayed reachable during scoped DROP")
            while guard.poll() is None:
                if not degraded_collector:
                    degraded_collector = healthwatch.http_json(ready_url, 2, local=True)[0] == 503
                if origin and not failed_origin:
                    failed_origin = healthwatch.public_ready(origin, 2)["state"] == "failed"
                time.sleep(.25)
            cleared = guard.stdout.readline().split()
            require(len(cleared) == 2 and cleared[0] == b"cleared", "no confirmed removal of the paper rule")
            elapsed = float(cleared[1]) - filter_start
        ended = time.monotonic()
        released_ns = time.time_ns()
        drill.report("network_fault_cleared", duration_seconds=round(elapsed, 2))
    require(elapsed >= args.duration - .2, "fault stopped before requested duration")
    require(degraded_collector and (not origin or failed_origin), "missing observed collector or origin failure")
    require(tcp(), "paper TCP did not reopen")
    paper_probe(release, args.protocol_fixture)
    target = (released_ns // NS_MINUTE + 1) * NS_MINUTE
    deadline = ended + 300
    recovery = {}
    while time.monotonic() < deadline:
        end = marks(state / "watermarks.json")
        events = transitions(alerts, cursor)
        market_recovered = any(completed(events, key, fault_wall, released_ns / 1e9) for key in MARKET_KEYS)
        market_ok = set(start) == set(end) == local_market(api) and healthwatch.http_json(ready_url, 2, local=True)[0] == 200
        origin_ok = not origin or healthwatch.public_ready(origin, 3)["state"] == "ok"
        elapsed = round(time.monotonic() - ended, 2)
        if market_ok:
            recovery.setdefault("collector_ready_seconds", elapsed)
        if origin and origin_ok:
            recovery.setdefault("origin_ready_seconds", elapsed)
        if market_ok and min(end.values()) >= target:
            recovery.setdefault("post_outage_watermark_seconds", elapsed)
        if (market_ok and min(end.values()) >= target and market_recovered and origin_ok
                and (not origin or completed(events, "public:ready", fault_wall, released_ns / 1e9))):
            break
        time.sleep(1)
    else:
        raise TimeoutError("no verified market/Tunnel recovery, post-outage watermark or matching alert transitions: "
                           + str({k: [r["event"] for r in v] for k, v in events.items()}))
    bars = catalog_window(release, catalog, start, end)
    drill.report("network_recovery_verified", recovery_seconds=round(time.monotonic() - ended, 2), stages=recovery, bars=bars,
                 alerts={k: [(item["event"], item["timestamp"]) for item in records] for k, records in events.items()})
    smoke = HERE.parent / "e2e-smoke.sh"
    subprocess.run([smoke, "local", *(["--protocol-fixture"] if args.protocol_fixture else [])], check=True, timeout=260)
    if mode == "full":
        subprocess.run([smoke, "public", origin], check=True, timeout=260)
    drill.report("network_drill_complete", mode=mode, unverified=omitted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--paper-only", action="store_true")
    modes.add_argument("--protocol-fixture", action="store_true")
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--public-origin")
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
