"""Run bounded local/public HTTP acceptance checks against a running fertile deployment."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/fertile"
USER_AGENT = "fertile-e2e-smoke/" + (ROOT / "VERSION").read_text(encoding="utf-8").strip()
PUBLIC_ORIGIN = "https://fertile.viramoon.com"
CORE = {"postgres", "finance-api", "finance-collector", "finance-backtest-worker", "web", "healthwatch"}
PAPER_SERVICES = {"xvfb", "ib-gateway"}
JOB = {"strategy": "DemoStrategy", "instrument": "6EH4.XCME", "start": "2024-01-01T23:02:00Z",
       "end": "2024-01-01T23:06:00Z", "params": {}}


class SmokeError(RuntimeError):
    pass


def require(valid, message):
    if not valid:
        raise SmokeError(message)


def log(event, **fields):
    print(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields},
                     ensure_ascii=False), flush=True)


def public_origin(value):
    origin = value.removesuffix("/")
    if origin != PUBLIC_ORIGIN:
        raise argparse.ArgumentTypeError(f"pass the Ruler-managed HTTPS origin {PUBLIC_ORIGIN}")
    return origin


def token_from(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        mode = os.fstat(stream.fileno()).st_mode
        require(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600, "web secret must be a regular 0600 file")
        text = stream.read(8193)
    require(len(text) <= 8192, "web secret file is too large")
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    match = re.fullmatch(r"WEB_ADMIN_TOKEN=([A-Za-z0-9._~+/\-]+=*)", lines[0]) if len(lines) == 1 else None
    require(match is not None, "expected one WEB_ADMIN_TOKEN assignment")
    return match[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin_key(parsed):
    port = parsed.port
    # Keep userinfo and brackets in the authority so only the configured host can match.
    host = parsed.netloc
    if port is None:
        host = host.removesuffix(":")
        port = {"http": 80, "https": 443}.get(parsed.scheme)
    else:
        host = host.rsplit(":", 1)[0]
    return parsed.scheme, host.lower(), port


class HTTP:
    def __init__(self, origin, local):
        self.origin = origin
        self._origin_key = origin_key(urlsplit(origin))
        self.opener = build_opener(NoRedirect(), *([ProxyHandler({})] if local else []))

    def is_same_origin(self, parsed):
        try:
            return origin_key(parsed) == self._origin_key
        except ValueError:  # Invalid port.
            return False

    def read(self, path, *, data=None, headers=None, expected=200, json_body=True, asset=False):
        url = urljoin(self.origin + "/", path)
        parsed = urlsplit(url)
        require(self.is_same_origin(parsed), "refusing a cross-origin request")
        verb = "POST" if data is not None else "GET"
        req = Request(url, method=verb, headers=headers or {}, data=data)
        req.add_header("User-Agent", USER_AGENT)
        started = time.monotonic()
        try:
            response = self.opener.open(req, timeout=10)
        except HTTPError as err:
            # Never print upstream bodies or sensitive header values, including on redirects.
            with err:
                raise SmokeError(f"{verb} {parsed.path} returned HTTP {err.code}, expected {expected}") from None
        with response:
            require(response.status == expected, f"{verb} {parsed.path} returned HTTP {response.status}")
            body = response.read(512 if asset else 1_048_577)
            media = response.headers.get_content_type().lower()
            log("http", method=verb, path=parsed.path, status=response.status,
                ms=round((time.monotonic() - started) * 1000))
        if asset:
            return media, body
        require(len(body) <= 1_048_576, f"oversized response at {parsed.path}")
        if not json_body:
            return media, body
        require(media == "application/json", f"{parsed.path} did not return JSON")
        obj = json.loads(body)
        require(isinstance(obj, dict), f"{parsed.path} did not return a JSON object")
        return obj


def require_fixture_listener(pid, port=4002):
    require(pid > 1, "FERTILE_SMOKE_FIXTURE_PID must name the synthetic Gateway process")
    proc = Path("/proc") / str(pid)
    argv = (proc / "cmdline").read_bytes().split(b"\0")
    require((proc / "exe").resolve(strict=True).name.startswith("python") and
            any(Path(os.fsdecode(arg)).name == "peer.py" for arg in argv) and b"gateway" in argv,
            "the provided pid is not the Python gateway protocol fixture")
    sockets = set()
    for descriptor in (proc / "fd").iterdir():
        try:
            sockets.add(os.readlink(descriptor))
        except FileNotFoundError:
            continue
    address = f"0100007F:{port:04X}"
    records = [line.split() for line in Path("/proc/net/tcp").read_text().splitlines()[1:]]
    require(any(row[1] == address and row[3] == "0A" and f"socket:[{row[9]}]" in sockets for row in records),
            "the named protocol fixture does not own the loopback listener")


def require_fixture_environment(root, box):
    for location in {root, box / "fertile", Path("/home/box/fertile")}:
        require(not any(os.path.lexists(location / "secrets" / file) for file in ("ibkr-paper.env", "alerts.env")),
                "paper credentials or an alert destination are present; fixture mode refused")
    require(os.environ.get("IB_GATEWAY_ADDRESS", "127.0.0.1:4002") == "127.0.0.1:4002",
            "protocol fixture requires a loopback Gateway")
    require_fixture_listener(int(os.environ.get("FERTILE_SMOKE_FIXTURE_PID", "0")))


def await_stack(protocol_fixture=False, *, paper_only=False):
    required = CORE - {"healthwatch"} if paper_only else CORE
    deadline = time.monotonic() + 60
    while True:
        result = subprocess.check_output([CLI, "status", "--json"], timeout=10)
        snapshot = json.loads(result)
        services = snapshot["services"]
        require(required <= services.keys(), "local supervisor manifest is missing a required service")
        if protocol_fixture:
            require(PAPER_SERVICES <= services.keys() and all(
                services[name]["state"] == "disabled" and services[name]["pid"] == 0 for name in PAPER_SERVICES),
                "protocol fixture requires explicitly disabled Gateway and xvfb")
            require(not ({"cloudflared", "tunnel", "viramoon-site", "watchdog"} & services.keys()),
                    "protocol fixture must not manage external services")
        skipped, unhealthy = {}, []
        for name, svc in services.items():
            if paper_only and (name == "healthwatch" or name in PAPER_SERVICES):
                skipped[name] = svc["state"]
                continue
            if name in PAPER_SERVICES and svc["state"] in {"disabled", "missing_binary", "needs_credentials"}:
                skipped[name] = svc["state"]
                continue
            running = svc["state"] == "RUNNING" and svc["pid"] > 1
            ready_codes = (200, 503) if paper_only and name == "finance-collector" else (200,)
            endpoints_ok = ((svc.get("live_url") is None or svc["live"] == 200)
                            and (svc.get("ready_url") is None or svc["ready"] in ready_codes))
            gateway_ok = name != "ib-gateway" or svc.get("application_state") == "ready"
            if not (running and endpoints_ok and gateway_ok):
                unhealthy.append(f"{name}={svc['state']} (live={svc.get('live')}, ready={svc.get('ready')})")
        for name in ("finance-api", "finance-collector", "web"):
            if not services[name].get("live_url") or not services[name].get("ready_url"):
                unhealthy.append(name + " missing health endpoint")
        if snapshot["supervisor"] == "RUNNING" and not unhealthy:
            break
        require(time.monotonic() < deadline, f"local supervisor readiness timed out: {unhealthy}")
        time.sleep(.5)
    for name, svc in services.items():
        log("supervised service", name=name, state=svc["state"], pid=svc["pid"], live=svc.get("live"), ready=svc.get("ready"))
    web = urlsplit(services["web"]["ready_url"])
    require(web.scheme == "http" and web.hostname in ("127.0.0.1", "::1", "localhost")
            and web.username is None and web.password is None and web.port is not None,
            "refusing to send a local admin token outside the loopback web service")
    return f"http://{web.netloc}", skipped


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "script" and fields.get("src"):
            self.entries.append(("js", fields["src"], fields.get("type") == "module"))
        if tag == "link" and "stylesheet" in (fields.get("rel") or "").lower().split() and fields.get("href"):
            self.entries.append(("css", fields["href"], True))


def check_assets(http):
    media, body = http.read("/", json_body=False)
    require(media == "text/html", "the SPA entry must serve HTML")
    page = Assets()
    page.feed(body.decode("utf-8"))
    same_origin = []
    for kind, href, check_content in sorted(set(page.entries)):
        url = urljoin(http.origin + "/", href)
        parsed = urlsplit(url)
        if not http.is_same_origin(parsed):
            log("skipped cross-origin asset", kind=kind, host=parsed.hostname)
        elif check_content:
            same_origin.append((kind, url))
    require({kind for kind, _ in same_origin} == {"js", "css"},
            "expected built Vite JavaScript and CSS from the same origin")
    for kind, url in same_origin:
        media, data = http.read(url, asset=True)
        allowed = {"application/javascript", "text/javascript", "application/ecmascript"} if kind == "js" else {"text/css"}
        require(media in allowed and data.strip() and not data.lstrip().startswith(b"<"), f"invalid {kind} asset")


def timestamp(value):
    require(isinstance(value, str), "missing timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(stamp.tzinfo is not None, "expected a timestamp with timezone")
    return stamp


def summary(data):
    require(isinstance(data, dict), "missing backtest summary object")
    require(all(isinstance(data.get(k), str) and data[k] for k in
                ("run_id", "instrument", "strategy", "currency", "start", "end", "generated_at")), "incomplete backtest summary")
    require(isinstance(data.get("pnl"), str) and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", data["pnl"])
            and Decimal(data["pnl"]).is_finite(), "pnl must be an exact decimal JSON string")
    require(all(type(data.get(k)) is int and data[k] >= 0 for k in ("total_orders","total_positions")),
            "invalid backtest counts")
    require(timestamp(data["start"]) < timestamp(data["end"]), "invalid backtest interval")
    timestamp(data["generated_at"])
    return {k: data[k] for k in ("run_id", "instrument", "strategy", "pnl", "currency")}


def check_market(market):
    require(market.get("state") == "ready" and market.get("source") in {"replay", "ib-realtime", "ib-delayed"},
            "market must be ready with an explicit supported source")
    require(type(market.get("max_age_seconds")) is int and market["max_age_seconds"] > 0
            and isinstance(market.get("instruments"), list) and market["instruments"], "missing market freshness metadata")
    now = datetime.now(timezone.utc)
    require(-1 <= (now - timestamp(market["updated_at"])).total_seconds() <= 11, "expired collector heartbeat")
    for row in market["instruments"]:
        require(row.get("stale") is False and type(row.get("age_seconds")) is int
                and 0 <= row["age_seconds"] < market["max_age_seconds"]
                and -1 <= (now - timestamp(row["last_bar_at"])).total_seconds() < market["max_age_seconds"] + 1,
                "stale or absent market bar")
    log("market", source=market["source"], updated_at=market["updated_at"], instruments=market["instruments"])


def smoke(http, secret, skipped, protocol_fixture=False, *, paper_only=False):
    require(http.read("/healthz/live").get("status") == "ok", "web is not live")
    ready = http.read("/healthz/ready")
    require(ready.get("status") == "ready" and ready.get("checks")
            and all(c.get("ok") is True for c in ready["checks"]), "web readiness checks did not all pass")
    state = http.read("/api/v1/system/status")
    require(state["database"]["ok"] is True and state["finance"]["ok"] is True and state["finance"]["circuit"] == "closed",
            "database/finance/circuit unhealthy in web system status")
    require(state["web"].get("version") and state["web"].get("commit") not in (None,"","unknown"), "missing web build identity")
    system_market = state["market"]
    recovering = paper_only and system_market["state"] == "recovering"
    if recovering:
        require(system_market["fresh"] is False and system_market["source"] in {"ib", "ib-realtime", "ib-delayed"},
                "unhealthy recovering system market summary")
    else:
        require(system_market["fresh"] is True and system_market["state"] == "ready"
                and system_market["instrument_count"] > 0 and system_market["stale_count"] == 0,
                "unhealthy system market summary")
    log("system", web=state["web"], database=state["database"], finance=state["finance"], market=system_market)
    log("sample", **summary(http.read("/api/v1/backtests/sample")))
    market = http.read("/api/v1/market/status")
    if recovering:
        instruments = market.get("instruments")
        require(market.get("state") == "recovering" and isinstance(instruments, list)
                and all(isinstance(row, dict) and type(row.get("stale")) is bool for row in instruments)
                and system_market["stale_count"] == sum(row["stale"] for row in instruments),
                "inconsistent recovering market status")
        log("paper market recovering", source=market["source"], stale_count=system_market["stale_count"])
    else:
        check_market(market)
    require(system_market["source"] == market["source"]
            and system_market["instrument_count"] == len(market["instruments"]),
            "market summaries disagree across status routes")
    if protocol_fixture:
        require(market["source"] in {"ib-realtime", "ib-delayed"}, "protocol fixture requires the IB collector")
        log("synthetic loopback IB protocol fixture", external_services=skipped,
            unverified=["IB Gateway login", "broker market data", "Cloudflare Tunnel", "public HTTPS"])
    elif skipped and not paper_only:
        require(market["source"] == "replay", "required external services are unavailable for paper acceptance")
        log("replay external services skipped", services=skipped)
    check_assets(http)

    key = "e2e-" + uuid4().hex
    log("submitting example job")
    accepted = http.read("/api/v1/backtests", data=json.dumps(JOB).encode(), expected=202,
                         headers={"Authorization": "Bearer " + token_from(secret), "Content-Type": "application/json",
                                  "Idempotency-Key": key})
    job_id = accepted.get("id")
    require(type(job_id) is int and job_id > 0, "web did not return an accepted job ID")
    phase = None
    while True:
        job = http.read(f"/api/v1/backtests/{job_id}")
        require(job["id"] == job_id and job["request"] == JOB, "web returned a different job")
        if phase != job["status"]:
            phase = job["status"]
            log("job", id=job_id, phase=phase)
        if phase == "succeeded":
            result = job["summary"]
            require(result["strategy"] == JOB["strategy"] and result["instrument"] == JOB["instrument"],
                    "job summary does not match the submission")
            log("completed", id=job_id, **summary(result))
            return
        require(phase in {"queued", "running"}, f"backtest {job_id} entered {phase}")
        time.sleep(1)


def research_smoke(http, secret, instrument, kind):
    base = "/api/v1/backtests"
    datasets = http.read(base + "/datasets")["items"]
    dataset = next((row for row in datasets if row["instrument"] == instrument), None)
    require(dataset is not None, f"missing explicitly requested research dataset: {instrument}")
    require(dataset["data_kind"] == kind, "research dataset does not match the specified source kind")
    request = {
        "strategy": "SmaCrossover", "instrument": instrument,
        "start": dataset["first_session"] + "T00:00:00Z",
        "end": dataset["last_session"] + "T00:00:00Z",
        "params": {"fast_period": 5, "slow_period": 20, "shares": 10},
    }
    headers = {"Authorization": "Bearer " + token_from(secret), "Content-Type": "application/json",
               "Idempotency-Key": "e2e-research-" + uuid4().hex}
    payload = json.dumps(request).encode()
    job_id = http.read(base, data=payload, headers=headers, expected=202)["id"]
    require(http.read(base + "/by-key", headers=headers)["id"] == job_id, "lookup returned a different job")
    require(http.read(base, data=payload, headers=headers, expected=202)["id"] == job_id, "same key created another job")
    log("research key lookup and duplicate submit matched", id=job_id, data_kind=kind)
    phase = None
    while True:
        job = http.read(f"{base}/{job_id}")
        require(job["request"] == request, "research request was changed")
        if phase != job["status"]:
            phase = job["status"]
            log("research job", id=job_id, phase=phase)
        if phase == "succeeded":
            result = job["summary"]
            report = result["research"]
            audit = report["audit"]
            require(result["strategy"] == request["strategy"] and result["instrument"] == instrument,
                    "research summary did not match the job")
            require(report["dataset"] == dataset and report["observations"] >= 21,
                    "invalid catalog provenance or insufficient samples")
            windows = report["windows"]
            require([row["window"] for row in windows] == ["50y", "10y", "5y", "longest_sample"]
                    and [row["requested_years"] for row in windows] == [50, 10, 5, None]
                    and all(row["coverage"] == "full" or row["metrics"] is None for row in windows),
                    "invalid historical window coverage")
            require(audit["fills"] == result["total_orders"] and
                    all(fill["signal_session"] < fill["execution_session"] for fill in audit["recent_fills"]),
                    "research fills failed the audit")
            log("research completed", id=job_id, data_kind=kind, source=dataset["source"], instrument=instrument,
                observed=report["observations"], actual=[report["actual_start"], report["actual_end"]],
                pnl=result["pnl"], currency=result["currency"], windows=[row["coverage"] for row in report["windows"]],
                fills=audit["fills"], zero_volume_bars=audit["zero_volume_bars"])
            return
        require(phase in {"queued", "running"}, f"research {job_id} entered {phase}: {job.get('error')}")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    local = modes.add_parser("local")
    local_profile = local.add_mutually_exclusive_group()
    local_profile.add_argument("--protocol-fixture", action="store_true",
                               help="isolated loopback IB wire test only; requires FERTILE_SMOKE_FIXTURE_PID")
    public = modes.add_parser("public")
    public.add_argument("origin", type=public_origin, help="explicitly confirmed HTTPS origin")
    for profile in (local_profile, public):
        profile.add_argument("--paper-only", action="store_true",
                             help="allow an IB market in recovery; locally skip Gateway, xvfb and healthwatch readiness")
    for mode in (local, public):
        mode.add_argument("--timeout-seconds", type=int, default=240, metavar="N", help="total wall-clock deadline (1..1800)")
        mode.add_argument("--research-instrument", help="also submit a historical job for this exact imported instrument")
        mode.add_argument("--research-kind", choices=("historical", "fixture"))
    args = parser.parse_args()
    require(bool(args.research_instrument) == bool(args.research_kind),
            "provide both --research-instrument and --research-kind")
    require(1 <= args.timeout_seconds <= 1800, "invalid total deadline")
    def timed_out(_signum, _frame):
        raise SmokeError("end-to-end smoke exceeded its total deadline")
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(args.timeout_seconds)
    try:
        box = Path(os.environ.get("FERTILE_BOX_HOME", "/home/box"))
        root = Path(os.environ.get("FERTILE_ROOT", box / "fertile"))
        fixture = args.mode == "local" and args.protocol_fixture
        if fixture:
            require_fixture_environment(root, box)
        origin, skipped = await_stack(fixture, paper_only=args.paper_only) if args.mode == "local" else (args.origin, {})
        secret = Path(os.environ.get("WEB_ADMIN_TOKEN_FILE", root / "secrets/web.env"))
        log("smoke", mode=args.mode, origin=origin, paper_only=args.paper_only, timeout_seconds=args.timeout_seconds)
        http = HTTP(origin, args.mode == "local")
        smoke(http, secret, skipped, fixture, paper_only=args.paper_only)
        if args.research_instrument:
            research_smoke(http, secret, args.research_instrument, args.research_kind)
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    try:
        main()
    except (SmokeError, OSError, ValueError, TypeError, KeyError, URLError, subprocess.SubprocessError) as exc:
        sys.exit(f"smoke failed: {exc}")
