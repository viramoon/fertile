"""Run IBC with a persistent, bounded paper-login budget."""
import configparser
import errno
import io
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from runtime_root import resolve_root

ROOT = resolve_root().resolve()
SECRET = ROOT / "secrets/ibkr-paper.env"
RUN = ROOT / "run/ibgateway"
DATA = ROOT / "data/ibgateway"
LOG = ROOT / "logs/ibgateway"
STATE = RUN / "state.json"
INI = RUN / "config.ini"
POLICY = ROOT / "tools/ibgateway/loopback.so"
DISPLAY = ":87"
BUDGET = 3
LOGIN_TIMEOUT = 150
ALLOWED = {"IBKR_USERNAME", "IBKR_PASSWORD"}


class GatewayError(Exception):
    """Messages are fixed codes safe for operator logs."""


def write_private(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    tmp.replace(path)


def state(name, attempts=0, code=None):
    result = dict(state=name, attempts=attempts, time=int(time.time()))
    if code:
        result["code"] = code
    write_private(STATE, json.dumps(result) + "\n")
    if code:
        with (LOG / "events.jsonl").open("a") as f:
            print(json.dumps(result), file=f)
    return result


def read_state():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {"state": "stopped", "attempts": 0}


@contextmanager
def locked(name, blocking=True):
    with (RUN / name).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def credentials(read=False):
    # Keep the credential lookup inside this deployment's private directory.
    if ROOT / "secrets/ibkr-paper.env" != SECRET or SECRET.parent.resolve() != SECRET.parent:
        raise GatewayError("needs_credentials")
    try:
        fd = os.open(SECRET, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise GatewayError("needs_credentials") from None
    except OSError:
        raise GatewayError("invalid_credentials_permissions") from None
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise GatewayError("invalid_credentials_permissions")
        if not read:
            return
        data = f.read(8193)
        if len(data) > 8192 or b"\0" in data:
            raise GatewayError("invalid_credentials_file")
        try:
            lines = data.decode().splitlines()
        except UnicodeError:
            raise GatewayError("invalid_credentials_file") from None
        values = {}
        for line in lines:
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if key not in ALLOWED or not sep or not value or key in values:
                raise GatewayError("invalid_credentials_file")
            values[key] = value
        if values.keys() != ALLOWED:
            raise GatewayError("invalid_credentials_file")
        return values


def property_value(value):
    # Preserve literal spaces, backslashes and non-ASCII without shell interpolation.
    raw = value.encode("utf-16-be")
    return "".join("\\u%04x" % int.from_bytes(raw[n:n+2], "big") for n in range(0, len(raw), 2))


def configure(values):
    properties = {
        "IbLoginId": property_value(values["IBKR_USERNAME"]),
        "IbPassword": property_value(values["IBKR_PASSWORD"]),
        "TradingMode": "paper", "ReadOnlyApi": "yes", "OverrideTwsApiPort": "4002",
        "ExistingSessionDetectedAction": "secondary", "AcceptIncomingConnectionAction": "reject",
        "ReloginAfterSecondFactorAuthenticationTimeout": "no", "ExitAfterSecondFactorAuthenticationTimeout": "yes",
        "SecondFactorAuthenticationTimeout": "120", "AutoRestartTime": "11:45 PM",
        "LogStructureWhen": "never", "CommandServerPort": "0",
    }
    write_private(INI, "".join(f"{key}={val}\n" for key, val in properties.items()))
    jts = DATA / "jts.ini"
    cfg = configparser.RawConfigParser(strict=False)
    cfg.optionxform = str
    cfg.read(jts)
    for section, options in {"IBGateway": {"TrustedIPs": "127.0.0.1", "ApiOnly": "true"},
                             "Logon": {"Locale": "en", "TimeZone": "UTC", "UseSSL": "true"}}.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
        for key, value in options.items():
            cfg.set(section, key, value)
    output = io.StringIO()
    cfg.write(output, space_around_delimiters=False)
    write_private(jts, output.getvalue())


def listeners():
    result = []
    for name in ("tcp", "tcp6"):
        path = Path("/proc/net") / name
        if name == "tcp6" and not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            addr, port = fields[1].split(":")
            if fields[3] != "0A" or int(port, 16) not in (4001, 4002):
                continue
            if int(port, 16) != 4002 or addr != "0100007F" or name != "tcp":
                raise GatewayError("unsafe_listener")
            result.append(addr)
    return bool(result)


def probe():
    if not listeners():
        raise GatewayError("not_listening")
    env = dict(os.environ, IB_GATEWAY_ADDRESS="127.0.0.1:4002", IB_PAPER_ACCOUNT_PREFIX="DU",
               FINANCE_CONFIG_DIR=str(ROOT / "current/finance/config"))
    with locked("probe.lock"):
        try:
            proc = subprocess.run([ROOT / "current/bin/finance-ib", "probe"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12, check=True)
            report = json.loads(proc.stdout)
            accounts = report["paper_accounts"]
            if not isinstance(accounts, list) or not accounts or not all(isinstance(a, str) and a.startswith("DU") for a in accounts):
                raise GatewayError("invalid_paper_identity")
            if not isinstance(report["server_time_unix"], int) or report["server_time_unix"] <= 0:
                raise GatewayError("invalid_server_time")
            return {"state": "ready", "address": "127.0.0.1:4002", "account_prefix": "DU",
                    "accounts": len(accounts), "server_time_unix": report["server_time_unix"]}
        except (subprocess.SubprocessError, ValueError, KeyError, OSError):
            raise GatewayError("probe_failed") from None


def display_environment():
    return dict(os.environ, DISPLAY=DISPLAY, XAUTHORITY=str(RUN / "Xauthority"), TZ="UTC")


def xvfb():
    credentials()
    if Path("/tmp/.X11-unix/X" + DISPLAY[1:]).exists():
        raise GatewayError("display_in_use")
    cookie = RUN / "Xauthority"
    write_private(cookie, "")
    # xauth reads the cookie on stdin; shell history and process arguments stay clean.
    subprocess.run(["xauth", "-q", "-f", str(cookie)], input=f"add {DISPLAY} . {secrets.token_hex(16)}\n",
                   text=True, check=True)
    os.execvpe("Xvfb", ["Xvfb", DISPLAY, "-screen", "0", "1024x768x24", "-nolisten", "tcp",
                         "-auth", str(cookie)], display_environment())


def stop_group(child):
    # ibcstart.sh launches a shell and a Java process. Clean the whole group.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            continue
    child.wait()


def verify_bind():
    # Exercise the *child's* dynamic loader and kernel bind result before giving IBC credentials.
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 4002))
        if sock.getsockname() != ("127.0.0.1", 4002):
            raise GatewayError("invalid_loopback_policy")
    for family, host, port in ((socket.AF_INET, "0.0.0.0", 4001), (socket.AF_INET6, "::", 4002)):
        try:
            with socket.socket(family) as sock:
                sock.bind((host, port))
        except OSError as err:
            if err.errno == errno.EACCES or (family == socket.AF_INET6 and err.errno == errno.EAFNOSUPPORT):
                continue
            raise
        raise GatewayError("invalid_loopback_policy")


def group_identity(pid):
    try:
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.getuid():
            return None
        # Fields after comm start at process state; require our recorded session leader.
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if int(fields[2]) == pid and int(fields[3]) == pid:
            return fields[19]
    except (FileNotFoundError, ProcessLookupError):
        pass
    return None


def reap_previous_group():
    marker = RUN / "process.json"
    if not marker.exists():
        return
    previous = json.loads(marker.read_text())
    pid = previous["pid"]
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        marker.unlink()
        return
    if not previous["start"] or group_identity(pid) != previous["start"]:
        raise GatewayError("orphaned_gateway_group")
    os.killpg(pid, signal.SIGTERM)
    time.sleep(1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    marker.unlink()


def startup():
    with locked("launcher.lock", blocking=False):
        try:
            reap_previous_group()
            gateway_loop()
        finally:
            INI.unlink(missing_ok=True)


def gateway_loop():
    info = read_state()
    attempts = int(info.get("attempts", 0))
    while True:
        if attempts >= BUDGET or info.get("state") == "needs_attention":
            if info.get("state") != "needs_attention":
                state("needs_attention", attempts, "login_or_2fa")
            # An explicit reset clears the budget. Leaving this process up prevents a supervisor respawn loop.
            while read_state().get("state") == "needs_attention":
                time.sleep(5)
            attempts = 0
            info = {}
        values = credentials(read=True)
        if listeners():
            raise GatewayError("port_in_use")
        gw_ver = os.environ["IB_GATEWAY_VERSION"]
        ibc_ver = os.environ["IBC_VERSION"]
        gw = ROOT / "tools/ibgateway" / gw_ver
        ibc = ROOT / "tools/ibc" / ibc_ver
        if not all(path.is_file() for path in (gw / "jre/bin/java", ibc / "scripts/ibcstart.sh",
                                              ibc / "IBC.jar", ROOT / "current/bin/finance-ib", POLICY)):
            raise GatewayError("needs_installation")
        # An ignored or broken LD_PRELOAD must prevent login, even when the file exists.
        env = dict(display_environment(), LD_PRELOAD=str(POLICY), JAVA_TOOL_OPTIONS="-Djava.net.preferIPv4Stack=true")
        checked = subprocess.run([sys.executable, __file__, "--verify-bind"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if checked.returncode:
            raise GatewayError("invalid_loopback_policy")
        configure(values)
        del values
        for _ in range(15):
            x_ready = subprocess.run(["xdpyinfo"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not x_ready.returncode:
                break
            time.sleep(1)
        else:
            raise GatewayError("xvfb_unavailable")
        args = [ibc / "scripts/ibcstart.sh", gw_ver, "--gateway", f"--tws-path={ROOT}/tools",
                f"--tws-settings-path={DATA}", f"--ibc-path={ibc}", f"--ibc-ini={INI}",
                f"--java-path={gw}/jre/bin", "--mode=paper", "--on2fatimeout=exit"]
        attempts += 1
        state("startup", attempts)
        deadline = time.monotonic() + LOGIN_TIMEOUT
        # Vendor output can include identifiers. Only our own redacted events are retained.
        child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            marker = RUN / "process.json"
            write_private(marker, json.dumps({"pid": child.pid, "start": group_identity(child.pid)}))
            while child.poll() is None:
                credentials()
                try:
                    probe()
                except GatewayError as err:
                    if str(err) in ("unsafe_listener", "invalid_paper_identity"):
                        raise
                    if time.monotonic() >= deadline:
                        break
                else:
                    if attempts:
                        attempts = 0
                        state("ready")
                    deadline = time.monotonic() + 45
                time.sleep(5)
        finally:
            stop_group(child)
            marker.unlink(missing_ok=True)
            INI.unlink(missing_ok=True)
        attempts = max(attempts, 1)
        if attempts < BUDGET:
            state("backoff", attempts, "gateway_disconnected")
            time.sleep(5 if attempts == 1 else 20)


def main(command):
    os.umask(0o077)
    for path in (RUN, DATA, LOG):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if command == "reset":
            if read_state().get("state") != "needs_attention":
                raise GatewayError("reset_requires_attention_state")
            print(json.dumps(state("stopped")))
        elif command == "status":
            credentials()
            print(json.dumps(read_state()))
        elif command == "check":
            credentials()
            current = read_state()
            if current["state"] == "needs_attention":
                print(json.dumps(current))
                return 1
            print(json.dumps(probe()))
        elif command == "xvfb":
            xvfb()
        elif command == "run":
            credentials()
            startup()
        else:
            print("Usage: ibgateway.sh {xvfb|run|check|status|reset}", file=sys.stderr)
            return 2
    except BlockingIOError:
        print(json.dumps({"state": "already_running"}))
        return 1
    except (GatewayError, OSError, ValueError, configparser.Error, subprocess.SubprocessError) as err:
        name = str(err) if isinstance(err, GatewayError) else "invalid_runtime_configuration"
        if command == "run":
            if name == "needs_credentials":
                state(name)
            else:
                state("needs_attention", BUDGET, name)
        print(json.dumps({"state": name}))
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--verify-bind"]:
        verify_bind()
        sys.exit(0)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    sys.exit(main(sys.argv[1] if len(sys.argv) == 2 else ""))
