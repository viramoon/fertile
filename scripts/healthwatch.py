"""Service probes, transition alerts and the daily backup scheduler."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as clock, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml

from http_origin import parse_https_origin

SCRIPTS = Path(__file__).resolve().parent
USER_AGENT = "fertile-healthwatch/" + (SCRIPTS.parent / "VERSION").read_text(encoding="utf-8").strip()
UTC = timezone.utc
COOLDOWN = 15 * 60


def now():
    return datetime.now(UTC)


def iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def result(state, detail, **numbers):
    return {"state": state, "detail": detail, **numbers}


def measured(ok, detail, **numbers):
    return result("ok" if ok else "failed", detail, **numbers)


def atomic_text(path, content):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        try:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)


def append(path, record):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def settings(root):
    deploy = SCRIPTS.parent / "deploy"
    conf = yaml.safe_load((deploy / "healthwatch.yaml").read_text())
    conf.update(public_origin=None, disk_path=str(root.parent))
    overlay = os.environ.get("FERTILE_HEALTH_CONFIG")
    if overlay:
        extra = yaml.safe_load(Path(overlay).read_text())
        if not isinstance(extra, dict) or extra.keys() - conf.keys():
            raise ValueError("invalid healthwatch config keys")
        conf.update(extra)
    if os.environ.get("FERTILE_PUBLIC_ORIGIN"):
        conf["public_origin"] = os.environ["FERTILE_PUBLIC_ORIGIN"]
    for key in ("interval_seconds", "timeout_seconds", "disk_min_free_bytes", "backup_max_age_seconds"):
        if type(conf[key]) is not int or conf[key] <= 0:
            raise ValueError("invalid positive integer: " + key)
    if conf["backup_at_utc"] is not None:
        datetime.strptime(conf["backup_at_utc"], "%H:%M")
    if not Path(conf["disk_path"]).is_absolute():
        raise ValueError("disk_path must be absolute")
    if conf["public_origin"] not in (None, ""):
        try:
            parse_https_origin(conf["public_origin"])
        except ValueError:
            raise ValueError("public_origin must be an HTTPS origin") from None
    return conf


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def http_json(url, timeout, local=False):
    opener = build_opener(ProxyHandler({}), NoRedirect()) if local else build_opener(NoRedirect())
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    try:
        with opener.open(Request(url, headers=headers), timeout=timeout) as resp:
            payload = resp.read(262145)
            if len(payload) > 262144:
                raise ValueError("oversized JSON")
            return resp.status, json.loads(payload)
    except HTTPError as error:
        return error.code, None
    except (OSError, URLError, ValueError):
        return 0, None


def public_ready(origin, timeout):
    if not origin:
        return result("skipped", "public origin not configured")
    code, body = http_json(origin.rstrip("/") + "/healthz/ready", timeout)
    ok = (code == 200 and isinstance(body, dict) and body.get("status") in ("ready", "degraded")
          and isinstance(body.get("checks"), list) and bool(body["checks"])
          and all(isinstance(c, dict) and isinstance(c.get("ok"), bool)
                  and isinstance(c.get("required"), bool) and (not c["required"] or c["ok"]) for c in body["checks"]))
    if code != 200:
        return result("failed", "HTTP " + str(code) if code else "HTTPS/JSON unavailable")
    return measured(ok, "HTTP 200 " + (str(body["status"]) if ok else "invalid web readiness response"))


def services():
    # The supervisor command probes configured live/ready URLs concurrently and
    # reports skipped services, process state and PID without exposing secrets.
    data = subprocess.run([SCRIPTS / "fertile", "status", "--json"], capture_output=True,
                          check=True, timeout=12)
    return json.loads(data.stdout)


def service_checks(report):
    checks = {"supervisor:process": measured(report["supervisor"] == "RUNNING", report["supervisor"])}
    for name, item in report["services"].items():
        disabled = not item["enabled"] or item["state"] == "disabled"
        base = "service:" + name + ":"
        # supervisord's startsecs grace includes the first round of this very sampler.
        running = item["state"] == "RUNNING" or (
            name == "healthwatch" and item["state"] == "STARTING" and item["pid"] == os.getpid())
        checks[base + "process"] = (result("skipped", "disabled") if disabled else
                                   measured(running, item["state"], pid=item["pid"]))
        for kind in ("live", "ready"):
            if item.get(kind + "_url"):
                code = item[kind]
                checks[base + kind] = (result("skipped", "disabled") if disabled else
                                       measured(code == 200, "HTTP " + str(code) if code else "unreachable"))
    return checks


def market(rows, timeout):
    finance = rows.get("finance-api")
    collector = rows.get("finance-collector")
    if any(row is not None and (not row["enabled"] or row["state"] == "disabled")
           for row in (finance, collector)):
        return result("skipped", "finance API or collector disabled")
    url = (finance or {}).get("ready_url")
    if not url:
        return result("failed", "finance endpoint unavailable")
    host = urlsplit(url)
    code, body = http_json(urlunsplit((host.scheme, host.netloc, "/api/v1/market/status", "", "")),
                           timeout, local=True)
    if code == 404:
        return result("skipped", "market endpoint returned 404")
    if code != 200 or not isinstance(body, dict):
        return result("failed", "market HTTP " + str(code))
    items = body.get("instruments")
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return result("failed", "invalid market response")
    ages = [i["age_seconds"] for i in items if type(i.get("age_seconds")) is int and i["age_seconds"] >= 0]
    stale = sum(i.get("stale") is not False for i in items)
    return measured(body.get("state") == "ready" and bool(items) and len(ages) == len(items) and stale == 0,
                    str(body.get("state", "unknown")), instrument_count=len(items), stale_count=stale,
                    max_age_seconds=max(ages, default=None), source=body.get("source"))


def latest_backup(root):
    backup_root = root / "backups"
    if backup_root.exists():
        for entry in sorted(backup_root.iterdir(), reverse=True):
            try:
                instant = datetime.strptime(entry.name, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
            except ValueError:
                continue
            if not entry.is_symlink() and entry.is_dir() and (entry/"snapshot.json").is_file() and (entry/"SHA256SUMS").is_file():
                return instant
    return None


class Backup:
    def __init__(self, root):
        self.root = root
        self.last_attempt = root / "run/healthwatch-backup-attempt"
        self.child = None
        self.exit_status = None

    def schedule(self, conf, instant, latest, once):
        at = conf["backup_at_utc"]
        if at is None or once:
            return result("skipped", "disabled" if at is None else "one-shot sampler")
        if self.child:
            self.exit_status = self.child.poll()
            if self.exit_status is None:
                return result("ok", "backup running")
            self.child = None
        due = datetime.combine(instant.date(), clock.fromisoformat(at), UTC)
        if instant < due or (latest and latest >= due):
            return result("ok", "scheduled " + at + " UTC")
        previous = float(self.last_attempt.read_text()) if self.last_attempt.exists() else 0
        if instant.timestamp() - previous >= COOLDOWN:
            atomic_text(self.last_attempt, str(instant.timestamp()))
            with (self.root / "logs/healthwatch/backup.log").open("ab") as log:
                self.child = subprocess.Popen([SCRIPTS / "backup.sh"], stdin=subprocess.DEVNULL,
                                              stdout=log, stderr=subprocess.STDOUT)
            return result("ok", "backup started")
        return result("failed", "backup produced no completed snapshot; awaiting retry" if self.exit_status in (None, 0)
                      else "backup exited " + str(self.exit_status))


class Alerts:
    def __init__(self, path):
        self.path = path
        self.published = {}
        self.sent = {}
        path.touch(exist_ok=True)
        with path.open() as handle:
            for line in handle:
                record = json.loads(line)
                self.remember(record)

    def remember(self, record):
        event, key = record["event"], record["check"]
        if event in ("failure", "recovery"):
            self.published[key] = "failed" if event == "failure" else "ok"
            self.sent[key, event] = datetime.fromisoformat(record["timestamp"]).timestamp()

    def notify(self, instant, checks):
        for key, check in checks.items():
            state = check["state"]
            if state == "skipped" or state == self.published.get(key, "ok"):
                continue
            event = "failure" if state == "failed" else "recovery"
            if instant.timestamp() - self.sent.get((key, event), 0) < COOLDOWN:
                continue
            timestamp = iso(instant)
            eid = hashlib.sha256(f"{timestamp}/{key}/{event}".encode()).hexdigest()[:24]
            record = {"timestamp": timestamp, "id": eid, "check": key, "event": event, "detail": check["detail"]}
            append(self.path, record)
            self.remember(record)


def webhook_url(root):
    path = root / "secrets/alerts.env"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError("alerts.env must be an owner-only 0600 regular file")
    values = [s.strip() for s in path.read_text().splitlines() if s.strip() and not s.lstrip().startswith("#")]
    if not values:
        return None
    if len(values) != 1 or not values[0].startswith("ALERT_WEBHOOK_URL="):
        raise ValueError("invalid alerts.env")
    url = values[0].split("=", 1)[1].strip().strip("\"'")
    if url and (urlsplit(url).scheme != "https" or not urlsplit(url).hostname):
        raise ValueError("webhook must be HTTPS")
    return url or None


class Delivery:
    def __init__(self, root, alerts):
        self.cursor = root / "run/healthwatch-webhook.cursor"
        self.path = alerts
        self.offset = int(self.cursor.read_text()) if self.cursor.exists() else alerts.stat().st_size
        self.result = result("skipped", "webhook disabled")
        # Persist the first offset before any new alerts can appear or be interrupted.
        atomic_text(self.cursor, str(self.offset))

    def drain(self, root, timeout):
        try:
            url = webhook_url(root)
            size = self.path.stat().st_size
            if self.offset > size:
                self.offset = 0
            if not url:
                if self.offset != size:
                    self.offset = size
                    atomic_text(self.cursor, str(size))
                self.result = result("skipped", "webhook disabled")
                return
            start = time.monotonic()
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                for _ in range(8):
                    line = handle.readline()
                    if not line:
                        break
                    record = json.loads(line)
                    if record["check"] != "webhook:delivery":
                        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
                                   "Idempotency-Key": record["id"]}
                        req = Request(url, data=line, headers=headers, method="POST")
                        with build_opener(NoRedirect()).open(req, timeout=timeout):
                            pass
                    self.offset = handle.tell()
                    atomic_text(self.cursor, str(self.offset))
                    if time.monotonic() - start >= timeout:
                        break
            self.result = result("ok", "delivery available")
        except (ValueError, OSError, URLError) as exc:
            # URLs may contain credentials. Never record the URL or exception text.
            self.result = result("failed", "delivery failed: " + type(exc).__name__)


def observe(root, conf, backup, delivery, once):
    instant = now()
    checks = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        public = pool.submit(public_ready, conf["public_origin"], conf["timeout_seconds"])
        try:
            report = services()
            checks.update(service_checks(report))
            checks["market:fresh"] = market(report["services"], conf["timeout_seconds"])
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            checks["supervisor:process"] = result("failed", "status unavailable: " + type(exc).__name__)
            checks["market:fresh"] = result("failed", "status unavailable")
        checks["public:ready"] = public.result()
    try:
        disk = os.statvfs(conf["disk_path"])
        free = disk.f_bavail * disk.f_frsize
        checks["disk:free"] = measured(free >= conf["disk_min_free_bytes"], f"{free} bytes free", free_bytes=free)
    except OSError as exc:
        checks["disk:free"] = result("failed", "filesystem unavailable: " + type(exc).__name__)
    try:
        latest = latest_backup(root)
        age = int((instant - latest).total_seconds()) if latest else None
        checks["backup:age"] = measured(age is not None and 0 <= age <= conf["backup_max_age_seconds"],
                                        "no complete backup" if age is None else f"{age}s", age_seconds=age)
        checks["backup:schedule"] = backup.schedule(conf, instant, latest, once)
    except (OSError, ValueError) as exc:
        checks.setdefault("backup:age", result("failed", "snapshot directory unavailable"))
        checks["backup:schedule"] = result("failed", "backup scheduler error: " + type(exc).__name__)
    checks["webhook:delivery"] = delivery.result
    checks["healthwatch:runtime"] = result("ok", "sample recorded")
    return checks


def run(root, conf, once=False):
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    for directory in ("run", "logs/alerts", "logs/healthwatch"):
        (root/directory).mkdir(parents=True, exist_ok=True)
    with (root/"run/healthwatch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        alerts = Alerts(root/"logs/alerts/alerts.jsonl")
        delivery = Delivery(root, alerts.path)
        backup = Backup(root)
        delivery.drain(root, conf["timeout_seconds"])
        while not stop.is_set():
            start = time.monotonic()
            try:
                checks = observe(root, conf, backup, delivery, once)
            except Exception as exc:
                checks = {"healthwatch:runtime": result("failed", "sampling exception: " + type(exc).__name__)}
                print(checks["healthwatch:runtime"]["detail"], file=sys.stderr, flush=True)
            instant = now()
            record = {"timestamp": iso(instant), "interval_seconds": conf["interval_seconds"], "checks": checks,
                      "profile": os.environ.get("FERTILE_PROFILE", "cloudbox"),
                      "market_source": os.environ.get("FERTILE_MARKET_SOURCE", "ib"),
                      "release": (root/"current").resolve().name if (root/"current").is_symlink() else None}
            append(root/"logs/healthwatch/samples.jsonl", record)
            alerts.notify(instant, checks)
            delivery.drain(root, conf["timeout_seconds"])
            if once:
                print(json.dumps(record, ensure_ascii=False))
                return
            stop.wait(max(0, conf["interval_seconds"] - (time.monotonic() - start)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="sample and record once; skip scheduled backup")
    args = parser.parse_args()
    root = Path(os.environ.get("FERTILE_ROOT", "/home/box/fertile"))
    if not root.is_absolute() or not root.is_dir():
        parser.error("FERTILE_ROOT must be an existing absolute directory")
    run(root, settings(root), args.once)
