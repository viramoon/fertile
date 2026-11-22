"""Local, single-user supervisor control through its private Unix-domain RPC socket."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from pwd import getpwuid
import shlex
import shutil
import signal
import stat
from string import Template
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener
from xmlrpc.client import Fault, ServerProxy

from supervisor.xmlrpc import SupervisorTransport
import yaml


SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("FERTILE_ROOT", "/home/box/fertile")).resolve()
RUN = ROOT / "run"
LOGS = ROOT / "logs"
CONF = RUN / "supervisord.conf"
SNAPSHOT = RUN / "services.json"
BINDIR = Path(os.environ.get("FERTILE_SUPERVISOR_BIN", ROOT / "tools/supervisor/bin"))
NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*\Z")
SETTING = re.compile(r"[A-Z_][A-Z_0-9]*\Z")
SUPPORTED = {
    "FERTILE_PROFILE", "FERTILE_MARKET_SOURCE", "FERTILE_DATA_ROOT", "FERTILE_POSTGRES_PORT",
    "FINANCE_API_PORT", "FINANCE_COLLECTOR_PORT", "WEB_PORT", "IB_GATEWAY_ADDRESS",
    "FINANCE_COLLECTOR_CONFIG", "WEB_REQUIRE_FINANCE", "FERTILE_HEALTH_CONFIG", "FERTILE_PUBLIC_ORIGIN",
    "FERTILE_BACKUP_DAYS", "FERTILE_BACKUP_WEEKS", "FERTILE_BACKUP_REMOTE",
}
INHERIT = {
    *SUPPORTED, "HOME", "PATH", "LANG", "LC_ALL", "TZ", "USER", "LOGNAME",
    "FERTILE_BOX_HOME", "FERTILE_ROOT", "LD_LIBRARY_PATH", "UV_CACHE_DIR",
    "FERTILE_CONTROL_PYTHON", "FERTILE_SUPERVISOR_BIN", "FERTILE_SERVICE_MANIFEST",
    "SSL_CERT_FILE", "CURL_CA_BUNDLE", "XDG_RUNTIME_DIR", "PYTHONDONTWRITEBYTECODE",
}


def atomic(path, content):
    encoded = content.encode()
    if path.is_file() and path.read_bytes() == encoded:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def read_env(path, secret=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (secret and
            (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid())):
            raise ValueError(f"expected a user-owned mode 0600 regular file: {path}")
        result = {}
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not SETTING.fullmatch(key):
                raise ValueError(f"invalid assignment in {path}")
            if value.startswith(('"', "'")) and value.endswith(value[0]):
                value = value[1:-1]
            result[key] = value
        return result


def connection():
    transport = SupervisorTransport(None, None, f"unix://{RUN}/supervisor.sock")
    return ServerProxy("http://localhost/RPC2", transport=transport).supervisor


def daemon_pid():
    try:
        return connection().getPID()
    except (OSError, Fault):
        return 0


def release_path():
    current = ROOT / "current"
    if not current.exists() and os.environ.get("FERTILE_SERVICE_MANIFEST"):
        return SCRIPTS.parent
    resolved = current.resolve(strict=True)
    if not current.is_symlink() or resolved.parent != (ROOT / "releases").resolve():
        raise ValueError("current must link to a directory under releases/")
    return resolved


def check_secret(path):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return "needs_credentials"
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid():
        return "invalid_credentials_permissions"
    return ""


def tunnel_config(path):
    if not path.is_file():
        return "needs_credentials"
    cfg = yaml.safe_load(path.read_text()) or {}
    cred = Path(str(cfg.get("credentials-file", ""))).expanduser()
    if not cred.is_absolute() or cred.resolve().parent != path.parent.resolve() or not cred.is_file():
        return "needs_credentials"
    if not any(isinstance(r, dict) and r.get("service") in ("http://127.0.0.1:8080", "http://localhost:8080")
               for r in cfg.get("ingress", [])):
        return "invalid_tunnel_ingress"
    return ""


def port(value):
    n = int(value)
    if not 1 <= n <= 65535:
        raise ValueError("port outside valid range")
    return str(n)


def build_plan():
    release = release_path()
    custom = os.environ.get("FERTILE_SERVICE_MANIFEST")
    manifest = Path(custom) if custom else release / "fertile/deploy/services.yaml"
    rows = yaml.safe_load(manifest.read_text())["services"]
    runtime = ROOT / "config/runtime.env"
    settings = read_env(runtime) if runtime.exists() else {}
    if settings.keys() - SUPPORTED:
        raise ValueError("unknown key in config/runtime.env: " + ", ".join(sorted(settings.keys() - SUPPORTED)))
    settings.update({k: os.environ[k] for k in SUPPORTED if k in os.environ})
    source = settings.get("FERTILE_MARKET_SOURCE", "ib")
    profile = settings.get("FERTILE_PROFILE", "cloudbox")
    if source not in ("ib", "replay") or profile not in ("cloudbox", "stable"):
        raise ValueError("unsupported profile or market source")
    default_data = ROOT / ("data/replay" if source == "replay" else "data")
    data = Path(settings.get("FERTILE_DATA_ROOT", default_data))
    if not data.is_absolute():
        raise ValueError("FERTILE_DATA_ROOT must be absolute")
    fields = dict(root=str(ROOT), release=str(release), finance_port=port(settings.get("FINANCE_API_PORT", 8081)),
                  collector_port=port(settings.get("FINANCE_COLLECTOR_PORT", 8082)), web_port=port(settings.get("WEB_PORT", 8080)),
                  cloud_config=str(Path(os.environ.get("FERTILE_BOX_HOME", "/home/box")) / ".cloudflared/config.yml"))
    common = dict(PYTHONDONTWRITEBYTECODE="1", FERTILE_ROOT=str(ROOT), FERTILE_PROFILE=profile, FERTILE_MARKET_SOURCE=source,
                  FERTILE_DATA_ROOT=str(data), PGHOST=str(RUN / "postgres"),
                  PGPORT=port(settings.get("FERTILE_POSTGRES_PORT", 5432)), PGDATABASE="web")
    common.update({key: val for key, val in settings.items() if key not in common})
    common["FERTILE_POSTGRES_PORT"] = common["PGPORT"]
    base = {k: os.environ[k] for k in INHERIT if k in os.environ}
    services = {}
    database = "external"
    expand = lambda item: Template(str(item)).substitute(fields)
    for row in rows:
        name = row["name"]
        if not NAME.fullmatch(name) or name in services:
            raise ValueError(f"invalid or duplicate service name: {name}")
        cmd = [expand(s) for s in row["command"]]
        directory = expand(row.get("directory", str(release)))
        env = {**base, **common}
        if name.startswith("finance-") or name == "healthwatch":
            env.update(FINANCE_CONFIG_DIR=str(release / "finance/config"), FINANCE_API_PORT=fields["finance_port"],
                       FINANCE_COLLECTOR_BIND=f"127.0.0.1:{fields['collector_port']}",
                       FINANCE_BUILD_ROOT=str(release / "runtime"), FINANCE_PYTHON=str(release / "runtime/venv/bin/python"),
                       FINANCE_COLLECTOR_CONFIG=settings.get("FINANCE_COLLECTOR_CONFIG", str(
                           release / "finance/config/collector" / (f"{profile}.yaml" if source == "replay" else "ib-cloudbox.yaml"))))
        if name == "web":
            # The controlling CLI can be newer than the selected release. Earlier
            # managed-role bundles shipped the SQL before declaring the capability.
            sql_available = (release / "fertile/scripts/prepare-web-db.sql").is_file()
            if not custom:
                declared = row.get("local_database_role")
                if declared and (declared != "fertile_web" or not sql_available):
                    raise ValueError("incomplete or unsupported web database provisioning in release")
                database = "managed" if sql_available else "legacy"
            authority = "fertile_web@localhost" if database == "managed" else "localhost"
            env.update(WEB_PORT=fields["web_port"], WEB_CONFIG_DIR=str(release / "web/config"),
                       WEB_STATIC_ROOT=str(release / "static"), WEB_ADMIN_TOKEN_FILE=str(ROOT / "secrets/web.env"),
                       DATABASE_URL=f"postgresql://{authority}/web?host={quote(common['PGHOST'], safe='')}&port={common['PGPORT']}",
                       FINANCE_BASE_URL=f"http://127.0.0.1:{fields['finance_port']}")
        env.update({k: expand(v) for k, v in row.get("environment", {}).items()})
        skip = "" if row.get("enabled", True) else "disabled"
        if not skip and row.get("ib"):
            skip = "disabled" if source == "replay" else check_secret(Path("/home/box/fertile/secrets/ibkr-paper.env"))
        if not skip and name == "cloudflared" and not custom:
            skip = tunnel_config(Path(fields["cloud_config"]))
        extra = [expand(s) for s in row.get("requires_bins", [])]
        if not skip and any(not shutil.which(binary, path=env.get("PATH")) for binary in [cmd[0], *extra]):
            skip = "missing_binary"
        if not skip and not Path(directory).is_dir():
            skip = "missing_directory"
        if not custom and not skip and name == "finance-collector":
            cfg = Path(env["FINANCE_COLLECTOR_CONFIG"])
            if not cfg.is_file():
                skip = "missing_configuration"
            elif yaml.safe_load(cfg.read_text()).get("source") != source:
                raise ValueError(f"{cfg}: collector source must match FERTILE_MARKET_SOURCE")
        env_file = row.get("env_file")
        if env_file:
            if Path(env_file).name != env_file or env_file == "ibkr-paper.env":
                raise ValueError("invalid service env_file")
            secret_path = ROOT / "secrets" / env_file
            secret_state = check_secret(secret_path)
            if row.get("env_file_optional") and secret_state == "needs_credentials":
                env_file = None
            else:
                skip = skip or secret_state
        services[name] = dict(command=cmd, directory=directory, env=env, env_file=env_file,
                              enabled=row.get("enabled", True), skip=skip,
                              live_url=expand(row["live"]) if row.get("live") else None,
                              ready_url=expand(row["ready"]) if row.get("ready") else None)
    web = services.get("web")
    if not custom and web and not web["skip"] and "postgres" in services:
        file = web["env_file"]
        external = file and "DATABASE_URL" in read_env(ROOT / "secrets" / file, secret=True)
        if external:
            database = "external"
        elif database == "managed":
            services["postgres"]["env"]["FERTILE_WEB_PEER"] = "1"
    return dict(release=str(release), data=str(data), create_data="FERTILE_DATA_ROOT" not in settings,
                default=not bool(custom), services=services, web_database=database)


def run_child(path, name):
    # Keep PID supervision effective when supervisord itself receives SIGKILL.
    parent = os.getppid()
    if parent == 1 or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) or os.getppid() != parent:
        raise RuntimeError("lost supervisor during startup")
    svc = json.loads(Path(path).read_text())
    env = svc["env"]
    if svc["env_file"]:
        env.update(read_env(ROOT / "secrets" / svc["env_file"], secret=True))
    os.chdir(svc["directory"])
    os.execvpe(svc["command"][0], svc["command"], env)


def esc(value):
    value = str(value)
    if any(c in value for c in "\r\n;#"):
        raise ValueError("unsupported supervisor config path character")
    return value.replace("%", "%%")


def render(plan):
    runtime = Path(plan["release"]) / "fertile" if plan["default"] else SCRIPTS.parent
    templates = runtime / "deploy/supervisor"
    program = Template((templates / "program.conf.tmpl").read_text())
    # Keep the child launcher stable when a release switches. External services
    # such as cloudflared have no dependency on a particular release tree.
    code = (runtime / "scripts/supervision.py").read_text()
    child_script = RUN / "programs" / ("launcher-" + hashlib.sha256(code.encode()).hexdigest() + ".py")
    atomic(child_script, code)
    chunks = []
    for idx, (name, svc) in enumerate(plan["services"].items(), 1):
        if svc["skip"]:
            continue
        logfile = LOGS / name / "stdout.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(svc, sort_keys=True)
        digest = hashlib.sha256(text.encode()).hexdigest()
        launcher = RUN / "programs" / f"{name}-{digest}.json"
        atomic(launcher, text)
        command = shlex.join([sys.executable, str(child_script), "_exec", str(launcher), name])
        chunks.append(program.substitute(name=name, priority=idx*10, directory=esc(svc["directory"]),
                                         command=esc(command), logfile=esc(logfile)))
    programs = RUN / "programs.conf"
    atomic(programs, "\n".join(chunks))
    template = Template((templates / "supervisord.conf.tmpl").read_text())
    atomic(CONF, template.substitute(run=esc(RUN), logs=esc(LOGS), programs=esc(programs)))
    atomic(SNAPSHOT, json.dumps(plan, indent=2))


def start_daemon():
    if daemon_pid():
        return
    env = {k: os.environ[k] for k in INHERIT if k in os.environ}
    subprocess.run([BINDIR / "supervisord", "-c", CONF], env=env, check=True)
    for _ in range(100):
        if daemon_pid():
            return
        time.sleep(.1)
    raise RuntimeError("supervisord did not open its private socket")


def stop_postgres(api, pid):
    """Promote smart shutdown to PostgreSQL's fast, transactional shutdown."""
    binary = shutil.which("postgres")
    if not binary or not pid:
        return False
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        proc = Path(f"/proc/{pid}")
        try:
            is_postmaster = os.path.samefile(proc / "exe", binary)
            parent = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except FileNotFoundError:
            return False
        if not is_postmaster or parent != api.getPID() or api.getProcessInfo("postgres")["pid"] != pid:
            return False
        # First ask supervisord to stop it: the autorestart policy must already
        # see an intentional stop before the postmaster receives fast shutdown.
        api.stopProcess("postgres", False)
        info = api.getProcessInfo("postgres")
        if info["statename"] == "STOPPING" and info["pid"] == pid:
            try:
                signal.pidfd_send_signal(fd, signal.SIGINT)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = api.getProcessInfo("postgres")
            if info["statename"] == "STOPPED":
                if info["exitstatus"] != 0:
                    raise RuntimeError(f"postgres shutdown exited with {info['exitstatus']}")
                return True
            time.sleep(.05)
        raise RuntimeError("postgres did not complete clean shutdown within 30 seconds")
    finally:
        os.close(fd)


def stop_group(api, name):
    info = api.getProcessInfo(name)
    state = info["statename"]
    if name == "postgres" and state == "RUNNING" and stop_postgres(api, info["pid"]):
        return
    if state in ("STARTING", "RUNNING", "BACKOFF", "STOPPING"):
        try:
            api.stopProcess(name, True)
        except Fault as error:
            if error.faultCode != 70:  # supervisor NOT_RUNNING
                raise


def down():
    if not daemon_pid():
        return
    api = connection()
    infos = api.getAllProcessInfo()
    order = list(json.loads(SNAPSHOT.read_text())["services"]) if SNAPSHOT.exists() else []
    names = {info["name"] for info in infos}
    for name in reversed(list(dict.fromkeys([*order, *sorted(names - set(order))]))):
        if name in names:
            stop_group(api, name)
    api.shutdown()
    for _ in range(350):
        if not daemon_pid():
            return
        time.sleep(.1)
    raise RuntimeError("supervisord shutdown timed out")


def prepare_web(plan):
    services = plan["services"]
    pg = services.get("postgres")
    if not plan["default"] or not pg or pg["skip"]:
        return
    web = services["web"]
    release = Path(plan["release"])
    database = plan.get("web_database")
    if database is None:  # restart may load a snapshot written by an older CLI
        if web.get("env_file") and "DATABASE_URL" in read_env(ROOT / "secrets" / web["env_file"], secret=True):
            return
        database = "managed" if (release / "fertile/scripts/prepare-web-db.sql").is_file() else "legacy"
    if database == "external":
        return
    admin = {**pg["env"], "PGUSER": getpwuid(os.getuid()).pw_name, "PGDATABASE": "postgres"}
    for _ in range(50):
        if subprocess.run(["pg_isready", "-q"], env=admin).returncode == 0:
            break
        time.sleep(.1)
    if database == "legacy":
        exists = subprocess.check_output(["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c",
            "SELECT 1 FROM pg_database WHERE datname='web'"], env=admin, text=True)
        if exists.strip() != "1":
            subprocess.run(["createdb", "web"], env=admin, check=True)
        return
    subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f",
                    release / "fertile/scripts/prepare-web-db.sql"], env=admin, check=True)
    admin["PGDATABASE"] = "web"
    # The initial web migration creates app and blog. Permission is limited to this
    # database and ends before the API starts. A retried ensure recovers an interrupted grant.
    revoke = ["psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-c", "REVOKE CREATE ON DATABASE web FROM fertile_web"]
    subprocess.run(revoke, env=admin, check=True)
    initial = subprocess.check_output(
        ["psql", "-XAt", "-c", "SELECT to_regnamespace('app') IS NULL AND to_regnamespace('blog') IS NULL"],
        env=admin, text=True).strip() == "t"
    try:
        if initial:
            subprocess.run(["psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-c",
                            "GRANT CREATE ON DATABASE web TO fertile_web"], env=admin, check=True)
        subprocess.run([release / "bin/web", "migrate"], cwd=web["directory"], env=web["env"], check=True)
    finally:
        if initial:
            subprocess.run(revoke, env=admin, check=True)
    subprocess.run(["psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-v", "finish=1", "-f",
                    release / "fertile/scripts/prepare-web-db.sql"], env=admin, check=True)


def ensure():
    plan = build_plan()
    svc = plan["services"]
    if plan["default"]:
        data = Path(plan["data"])
        if plan["create_data"] and data == ROOT / "data/replay" and (ROOT / "data").is_dir():
            data.mkdir(exist_ok=True)
        if not data.is_dir():
            raise ValueError(f"configured data root is unavailable: {data}")
        if "postgres" in svc and not svc["postgres"]["skip"]:
            subprocess.run([Path(plan["release"]) / "fertile/scripts/postgres.sh", "init"],
                           env=svc["postgres"]["env"], check=True)
    render(plan)
    start_daemon()
    api = connection()
    added, changed, removed = api.reloadConfig()[0]
    for name in reversed(list(dict.fromkeys([*svc, *removed]))):
        if name in changed or name in removed:
            stop_group(api, name)
            api.removeProcessGroup(name)
    failures = []
    for name, service in svc.items():
        if service["skip"]:
            print(f"{name}: {service['skip']}")
            continue
        if name in added or name in changed:
            api.addProcessGroup(name)
        state = api.getProcessInfo(name)["statename"]
        if state in ("RUNNING", "STARTING", "BACKOFF"):
            continue
        if state == "FATAL":
            failures.append(f"{name}: FATAL; inspect logs, then run fertile restart {name}")
            continue
        try:
            if name == "web":
                prepare_web(plan)
            api.startProcess(name, True)
        except (Fault, OSError, subprocess.CalledProcessError) as error:
            failures.append(f"{name}: {error}")
    if failures:
        raise RuntimeError("; ".join(failures))


def http_status(url):
    if not url:
        return None
    try:
        # Probes use only configured local services and bypass ambient HTTP proxies.
        with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (OSError, URLError):
        return 0


def report():
    if SNAPSHOT.exists():
        plan = json.loads(SNAPSHOT.read_text())
    elif not (ROOT / "current").exists() and not os.environ.get("FERTILE_SERVICE_MANIFEST"):
        rows = yaml.safe_load((SCRIPTS.parent / "deploy/services.yaml").read_text())["services"]
        plan = {"services": {row["name"]: {"enabled": row.get("enabled", True), "skip": "missing_release",
                            "live_url": None, "ready_url": None} for row in rows}}
    else:
        plan = build_plan()
    pid = daemon_pid()
    infos = {r["name"]:r for r in connection().getAllProcessInfo()} if pid else {}
    services = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        pending = {}
        for name, svc in plan["services"].items():
            info = infos.get(name, {})
            state = svc["skip"] or info.get("statename", "STOPPED")
            services[name] = dict(enabled=svc["enabled"], state=state, pid=info.get("pid", 0),
                                  live=None, ready=None, live_url=svc["live_url"], ready_url=svc["ready_url"])
            if name == "ib-gateway" and not svc["skip"] and state == "RUNNING":
                try:
                    gateway = json.loads((RUN / "ibgateway/state.json").read_text())
                    app_state = gateway.get("state")
                    if app_state in ("startup", "ready", "backoff", "needs_credentials", "needs_attention"):
                        services[name]["application_state"] = app_state
                        if gateway.get("code") == "login_or_2fa":
                            services[name]["application_code"] = "login_or_2fa"
                except (OSError, ValueError):
                    pass  # Process startup may precede the first published state.
            if not svc["skip"]:
                for check in ("live", "ready"):
                    pending[name,check] = pool.submit(http_status, svc[check+"_url"])
        for (name, check), result in pending.items():
            services[name][check] = result.result()
    return dict(supervisor="RUNNING" if pid else "STOPPED", supervisor_pid=pid, services=services)


def switch(link, target):
    with tempfile.TemporaryDirectory(dir=ROOT) as staging:
        temp = Path(staging) / "current"
        temp.symlink_to(target, target_is_directory=True)
        os.replace(temp, link)


def rollback(ref):
    if not re.fullmatch("[0-9a-f]{7,40}", ref):
        raise ValueError("expected a built commit")
    matches = [p for p in (ROOT/"releases").glob(ref+"*")
               if re.fullmatch("[0-9a-f]{40}", p.name) and not p.is_symlink() and p.is_dir()]
    if len(matches) != 1:
        raise ValueError("no unique built release for that commit")
    target, previous = matches[0], release_path()
    for item in ("fertile/scripts/fertile", "fertile/deploy/services.yaml", "bin/web", "bin/finance-api"):
        if not (target/item).is_file():
            raise ValueError(f"incomplete rollback release: {item}")
    recorded = json.loads((target / "manifest.json").read_text())
    components = recorded["components"]
    subprocess.run([sys.executable, SCRIPTS / "release-manifest.py", "verify", target, target.name,
                    *(components[k] for k in ("finance", "web", "frontend"))], check=True)
    if target == previous:
        ensure()
        return
    # Reconcile only changed groups, including when reversing a failed switch.
    switch(ROOT/"current", target)
    try:
        ensure()
    except Exception:
        switch(ROOT/"current", previous)
        ensure()
        raise


@contextmanager
def lock():
    RUN.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    with (RUN / "control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def main():
    parser = argparse.ArgumentParser(prog="fertile")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("up", "down", "ensure"):
        sub.add_parser(action)
    sub.add_parser("status").add_argument("--json", action="store_true")
    for action in ("logs", "restart", "stop"):
        sub.add_parser(action).add_argument("name")
    sub.add_parser("rollback").add_argument("ref")
    args = parser.parse_args()
    if args.action == "status":
        data = report()
        if args.json:
            print(json.dumps(data))
        else:
            print(f"supervisord {data['supervisor']} pid={data['supervisor_pid']}")
            for name, item in data["services"].items():
                extra = " " + item["application_state"] if "application_state" in item else ""
                print(f"{name:24} {item['state']:24} pid={item['pid']} live={item['live']} ready={item['ready']}{extra}")
        return
    if args.action == "logs":
        if not NAME.fullmatch(args.name):
            raise ValueError("invalid service name")
        os.execlp("tail", "tail", "-n", "100", str(LOGS / args.name / "stdout.log"))
    with lock():
        if args.action not in ("down", "stop") and (RUN / "restore-journal.json").exists():
            raise RuntimeError("unfinished restore; run scripts/restore.sh to recover before starting services")
        if args.action in ("up", "ensure"):
            ensure()
        elif args.action == "down":
            down()
        elif args.action == "rollback":
            rollback(args.ref)
        else:
            if not daemon_pid():
                if args.action == "stop":
                    return
                raise ValueError("supervisord is stopped; use fertile ensure")
            plan = json.loads(SNAPSHOT.read_text())
            svc = plan["services"].get(args.name)
            if not svc or svc["skip"]:
                raise ValueError(f"service unavailable: {args.name}")
            api = connection()
            stop_group(api, args.name)
            if args.action == "stop":
                return
            if args.name == "web":
                prepare_web(plan)
            api.startProcess(args.name, True)


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["_exec"]:
            run_child(*sys.argv[2:])
        else:
            main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError, Fault) as exc:
        sys.exit(f"fertile: {exc}")
