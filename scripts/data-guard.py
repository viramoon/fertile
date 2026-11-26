#!/usr/bin/env python3
"""Offline data publication checks for the fertile Git repository and CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


RULES = "policy/data-guard.json"
LICENSES = "policy/market-data-licenses.json"
APPROVALS = "policy/market-data-approvals.json"
REGULAR_FILE_MODES = {"100644", "100755"}
USES = {"display_live", "record_local", "derive_non_display", "backup_personal_drive"}
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PRODUCT_REFERENCE = re.compile(r"https://[a-zA-Z0-9.-]+(?::443)?/[a-zA-Z0-9._~/-]*")
PRICE = re.compile(r"(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,6})?")
# Many of these headers are binary. Check ASCII headers too before decoding.
MAGIC = (
    b"PAR1", b"PARE", b"SQLite format 3\0", b"PGDMP", b"ARROW1", b"ORC",
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"%PDF-",
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"BZh",
    b"\xfd7zXZ\0", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"\x28\xb5\x2f\xfd",
    b"\x7fELF", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
)
CREDENTIAL_TOKEN_PATTERN = r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})"
PATH_TOKEN = re.compile(CREDENTIAL_TOKEN_PATTERN, re.IGNORECASE)
TEXT_RULES = {
    "lfs-pointer": r"(?im)^\s*version\s+https://git-lfs\.github\.com/spec/v1\s*$",
    "attachment": (
        r"(?i)!\[[^\]\n]*\]\s*(?:\(|\[)|<\s*(?:img|picture|video|svg)\b"
        r"|data:(?:image|video)/|(?:user-images|private-user-images)\.githubusercontent\.com/"
        r"|(?m:^[ \t]*(?:iVBORw0KGg[opqr]|/9j/|R0lGOD|UEsDB|H4sI)[A-Za-z0-9+/]{32,}={0,2}[ \t]*$)"
        r"|github\.com/(?:user-attachments/|[^/\s]+/[^/\s]+/(?:files/|assets/|actions/runs/\d+/artifacts/))"
    ),
    "database-or-log-export": (
        r"(?im)^(?:-- (?:PostgreSQL database dump|MySQL dump)|\s*(?:COPY\s+\S+.+\sFROM stdin;"
        r"|INSERT INTO\s+\S+\s+VALUES\s*\(\s*['\d]))"
        r"|^\s*\d{4}-\d\d-\d\d[T ][\d:.+Z-]+[ \t]+(?:\[[A-Z]+\]|TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR)\b"
    ),
    "credential": (
        r"(?im)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
        rf"|(?<!\w){CREDENTIAL_TOKEN_PATTERN}(?!\w)"
        r"|^[ \t]*(?:export[ \t]+)?(?:WEB_ADMIN_TOKEN|IBKR_PASSWORD|IBKR_USERNAME|DATABASE_URL|AWS_SECRET_ACCESS_KEY)"
        r"[ \t]*=[ \t]*['\"]?[A-Za-z0-9_@.:/+\-=!%?&*#\[\]]{8,}['\"]?[ \t]*(?:\#[^\r\n]*)?$"
    ),
}
TEXT_RULES = {name: re.compile(pattern) for name, pattern in TEXT_RULES.items()}
MARKET_HEADER = re.compile(
    r"(?im)^\s*[\"']?(?:symbol|instrument(?:_id)?|timestamp|ts(?:_event)?|date)[\"']?[,\t;]"
    r"(?=[^\r\n]*(?:price|size|bid|ask|open|high|low|close|volume)\b)[^\r\n]+"
)
MARKET_JSON = re.compile(
    r'(?is)"(?:price|bid(?:_price)?|ask(?:_price)?|open|high|low|close)"\s*:\s*"?-?\d+(?:\.\d+)?'
)
MARKET_CONTEXT = re.compile(
    r'(?is)"(?:symbol|instrument(?:_id)?|timestamp|ts(?:_event)?|volume|size|high|low)"\s*:\s*'
    r'(?:"[^"\r\n]+"|-?\d+(?:\.\d+)?)'
)
MARKET_ROW = re.compile(
    r"(?m)^[ \t]*(?:\|[ \t]*)?(?:[A-Z0-9_.-]+[,;\t|][ \t]*)?"
    r"\d{4}-\d\d-\d\d(?:[T ][\d:.+Z-]+)?[ \t]*[,;\t|][ \t]*"
    r"-?\d+(?:\.\d+)?[ \t]*[,;\t|][ \t]*-?\d+(?:\.\d+)?"
)
MARKDOWN_QUOTE = re.compile(r"(?m)^(?:[ \t]*>[ \t]*)+")
MARKDOWN_SEPARATOR = re.compile(r"\|?[ \t]*:?-+:?(?:[ \t]*\|[ \t]*:?-+:?)+[ \t]*\|?")
MARKET_TABLE_KEYS = {"symbol", "ticker", "instrument", "instrument_id", "timestamp", "ts", "ts_event", "date",
                     "标的", "合约", "时间戳", "日期"}
MARKET_TABLE_VALUES = {"price", "size", "quantity", "bid", "ask", "bid_price", "ask_price",
                       "open", "high", "low", "close", "volume", "价格", "数量", "成交量"}
GIT_ENV = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1"}


class Rejected(Exception):
    """Report a fixed reason and an input category or Git object ID."""

    def __init__(self, reason, location=None):
        super().__init__(reason, location)
        self.reason = reason
        self.location = location


def git(*args, missing_ok=False):
    result = subprocess.run(["git", *args], env=GIT_ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        if missing_ok:
            return None
        raise Rejected("git-read-failed")
    return result.stdout


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def git_blob(oid, limit):
    if int(git("cat-file", "-s", oid)) > limit:
        raise Rejected("blob-too-large")
    return git("cat-file", "blob", oid)


def read_rules(entries, required=True):
    entry = entries.get(RULES)
    if entry is None:
        if required:
            raise Rejected("missing-rule-file")
        return None
    mode, oid = entry
    try:
        if mode not in REGULAR_FILE_MODES:
            raise ValueError("rule file must be regular")
        rules = json.loads(git_blob(oid, 1048576), object_pairs_hook=unique_object)
        if not isinstance(rules, dict):
            raise ValueError("rules must be an object")
    except (Rejected, ValueError, RecursionError):
        raise Rejected("invalid-rule-file") from None
    version = rules.get("schema_version")
    if type(version) is not int or version != 1:
        raise Rejected("unsupported-rule-version")
    return rules


def schema(data, collection):
    obj = json.loads(data, object_pairs_hook=unique_object)
    if (not isinstance(obj, dict) or set(obj) != {"schema_version", collection}
            or type(obj["schema_version"]) is not int or obj["schema_version"] != 1
            or not isinstance(obj[collection], dict)):
        raise ValueError("invalid policy schema")
    return obj[collection]


def repo_path(path):
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
        return False
    return all(part not in {"", ".", ".."} and all(c.isprintable() for c in part)
               for part in path.split("/"))


def contains_market_data(text):
    text = MARKDOWN_QUOTE.sub("", text)
    if (MARKET_HEADER.search(text) or MARKET_ROW.search(text)
            or (MARKET_JSON.search(text) and MARKET_CONTEXT.search(text))):
        return True
    previous = ""
    for line in text.splitlines():
        if "|" in line and MARKDOWN_SEPARATOR.fullmatch(line.strip()):
            columns = {cell.strip(" \t`*_").casefold() for cell in previous.split("|")}
            if columns & MARKET_TABLE_KEYS and columns & MARKET_TABLE_VALUES:
                return True
        previous = line
    return False


def text_findings(text, market=True):
    for reason, rule in TEXT_RULES.items():
        if rule.search(text):
            yield reason
    if market and contains_market_data(text):
        yield "market-data-shape"


def index_entries():
    entries = {}
    for row in filter(None, git("ls-files", "--stage", "-z").split(b"\0")):
        fields, name = row.split(b"\t", 1)
        mode, oid, stage = fields.decode("ascii").split()
        if stage != "0":
            raise Rejected("unmerged-path", oid)
        entries[os.fsdecode(name)] = (mode, oid)
    return entries


def tree_entries(oid):
    entries = {}
    for row in filter(None, git("ls-tree", "-r", "-z", oid).split(b"\0")):
        fields, name = row.split(b"\t", 1)
        mode, _, blob = fields.decode("ascii").split()
        entries[os.fsdecode(name)] = (mode, blob)
    return entries


class Guard:
    def __init__(self, rules):
        self.rules = rules
        self.limit = rules["max_blob_bytes"]
        self.private_dir = re.compile(rules["blocked_directory_pattern"])
        self.safe_fixtures = set(rules["fixture_extensions"])
        self.blocked_extensions = set(rules["blocked_extensions"])
        self.checked_blobs = set()
        self.checked_commits = set()

    def blob(self, oid):
        return git_blob(oid, self.limit)

    def path_findings(self, path, fixture):
        if not repo_path(path):
            yield "invalid-path"
        posix = PurePosixPath(path.lower())
        if any(self.private_dir.fullmatch(part) for part in posix.parts[:-1]):
            yield "private-directory"
        if (posix.name in self.rules["blocked_filenames"]
                or (posix.name.startswith(".env.") and posix.name != ".env.example")
                or posix.name == ".env"):
            yield "private-filename"
        if posix.suffix in self.blocked_extensions:
            yield "blocked-extension"
        if PATH_TOKEN.search(path) or any(TEXT_RULES["credential"].search(part) for part in path.split("/")):
            yield "credential"
        if (posix.suffix in self.safe_fixtures - {".json"}
                or (posix.suffix in self.safe_fixtures
                    and path.startswith(self.rules["fixture_root"]))) and not fixture:
            yield "missing-synthetic-source"

    def blob_findings(self, path, data, fixture):
        if (data.startswith(MAGIC) or data[257:262] == b"ustar"
                or (data.startswith(b"RIFF") and data[8:12] in (b"WEBP", b"WAVE", b"AVI "))):
            yield "file-signature"
            return
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            yield "opaque-binary"
            return
        if any((ord(c) < 32 and c not in "\t\r\n") or 127 <= ord(c) <= 159 for c in text):
            yield "opaque-binary"
            return
        if PurePosixPath(path).name == ".gitattributes" and any(
                re.search(r"\b(?:filter|diff|merge)=lfs\b", line)
                for line in text.splitlines() if not line.lstrip().startswith("#")):
            yield "lfs-attributes"
        yield from text_findings(text, market=not fixture)

    def fixture_entries(self, entries):
        manifest = self.rules["fixture_manifest"]
        if manifest not in entries:
            return {}
        try:
            fixtures = schema(self.blob(entries[manifest][1]), "fixtures")
            for path, entry in fixtures.items():
                if (not repo_path(path) or not path.startswith(self.rules["fixture_root"])
                        or PurePosixPath(path).suffix.lower() not in self.safe_fixtures
                        or path not in entries or entries[path][0] not in REGULAR_FILE_MODES
                        or not isinstance(entry, dict)
                        or set(entry) != {"source", "generator", "seed", "sha256"}
                        or entry["source"] != "synthetic"
                        or not isinstance(entry["seed"], str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", entry["seed"])
                        or not isinstance(entry["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                    raise ValueError("invalid fixture")
                generator = entry["generator"]
                if (not repo_path(generator) or generator not in entries
                        or entries[generator][0] not in REGULAR_FILE_MODES
                        or PurePosixPath(generator).suffix not in self.rules["generator_extensions"]
                        or any(self.path_findings(generator, False))):
                    raise ValueError("invalid generator")
            return fixtures
        except (Rejected, ValueError, UnicodeError, RecursionError):
            raise Rejected("invalid-fixture-manifest") from None

    def check_license_records(self, entries, require_policy):
        if not require_policy and LICENSES not in entries and APPROVALS not in entries:
            return
        location = LICENSES
        try:
            products = schema(self.blob(entries[LICENSES][1]), "products")
            if not products:
                raise ValueError("missing products")
            for name, product in products.items():
                if (not SLUG.fullmatch(name) or not isinstance(product, dict)
                        or set(product) != USES | {"redistribute", "basis"}
                        or product["redistribute"] is not False
                        or any(product[use] is not None and type(product[use]) is not bool for use in USES)
                        or not isinstance(product["basis"], str)
                        or (product["basis"] == "pending"
                            and any(product[use] is not None for use in USES))
                        or (product["basis"] != "pending"
                            and not PRODUCT_REFERENCE.fullmatch(product["basis"]))):
                    raise ValueError("invalid product")
            location = APPROVALS
            approvals = schema(self.blob(entries[APPROVALS][1]), "approvals")
            for name, approval in approvals.items():
                if (name not in products or not isinstance(approval, dict)
                        or set(approval) != {"capability", "price"}
                        or approval["capability"] not in ("pending", "approved", "declined")):
                    raise ValueError("invalid approval")
                price = approval["price"]
                if price is not None and (
                        not isinstance(price, dict) or set(price) != {"amount", "currency", "period"}
                        or not isinstance(price["amount"], str) or not PRICE.fullmatch(price["amount"])
                        or not isinstance(price["currency"], str)
                        or not re.fullmatch(r"[A-Z]{3}", price["currency"])
                        or price["period"] not in ("month", "year", "one-time")):
                    raise ValueError("invalid price")
        except (Rejected, KeyError, ValueError, UnicodeError, RecursionError):
            raise Rejected("invalid-policy-record", location) from None

    def check_tree(self, entries, require_policy=False):
        manifest = self.rules["fixture_manifest"]
        if require_policy and manifest not in entries:
            raise Rejected("missing-fixture-manifest")
        fixtures = self.fixture_entries(entries)
        self.check_license_records(entries, require_policy)
        for path, (mode, oid) in entries.items():
            if mode != "160000" and mode not in REGULAR_FILE_MODES:
                raise Rejected("non-regular-file", oid)
            fixture = fixtures.get(path)
            key = (path, oid, fixture["sha256"] if fixture else None)
            if key in self.checked_blobs:
                continue
            try:
                for reason in self.path_findings(path, fixture):
                    raise Rejected(reason)
                if mode == "160000":
                    continue  # The subrepo checks the blobs behind its linked revision.
                data = self.blob(oid)
                if fixture and hashlib.sha256(data).hexdigest() != fixture["sha256"]:
                    raise Rejected("synthetic-digest-mismatch")
                for reason in self.blob_findings(path, data, fixture):
                    raise Rejected(reason)
            except Rejected as error:
                raise Rejected(error.reason, oid) from None
            self.checked_blobs.add(key)

    def commit(self, oid, require_policy=False):
        key = (oid, require_policy)
        if key in self.checked_commits:
            return
        entries = tree_entries(oid)
        read_rules(entries, required=require_policy)
        self.check_tree(entries, require_policy)
        self.message(git("show", "--no-notes", "--format=%B", "--no-patch", oid), "commit-message")
        self.checked_commits.add(key)

    def message(self, data, location):
        if len(data) > self.rules["max_pr_bytes"]:
            raise Rejected("message-too-large", location)
        for reason in self.blob_findings(location, data, fixture=False):
            raise Rejected(reason, location)

    def range(self, head, base):
        if git("rev-parse", "--is-shallow-repository").strip() != b"false":
            raise Rejected("shallow-history")
        self.commit(head, require_policy=True)
        args = [head]
        if base:
            args.append("^" + base)
        for oid in git("rev-list", *args).decode("ascii").splitlines():
            if oid != head:
                self.commit(oid)


def pre_push(changes):
    for row in changes:
        fields = row.split()
        if len(fields) != 4 or not all(OID.fullmatch(fields[i]) for i in (1, 3)):
            raise Rejected("invalid-push-record")
        _, local, _, remote = fields
        if set(local) == {"0"}:
            continue
        head = commit_oid(local)
        guard = Guard(read_rules(tree_entries(head)))
        while local != head:
            tag = git("cat-file", "tag", local)
            guard.message(tag, "tag-annotation")
            target = tag.partition(b"\n")[0].removeprefix(b"object ").decode("ascii")
            if not OID.fullmatch(target):
                raise Rejected("invalid-tag-target")
            local = target
        base = commit_oid(remote, missing_ok=True) if set(remote) != {"0"} else None
        guard.range(head, base)


def commit_oid(ref, missing_ok=False):
    result = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}", missing_ok=missing_ok)
    return result.decode("ascii").strip() if result else None


def pr_metadata(event):
    try:
        pr = json.loads(event.read_bytes())["pull_request"]
        title, body = pr["title"], pr["body"]
        if not isinstance(title, str) or (body is not None and not isinstance(body, str)):
            raise ValueError("invalid PR text")
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        raise Rejected("invalid-pr-event") from None
    return (title + "\n" + (body or "")).encode()


def check_private_roots(roots):
    worktrees = [
        Path(os.fsdecode(row[len(b"worktree "):])).resolve()
        for row in git("worktree", "list", "--porcelain", "-z").split(b"\0")
        if row.startswith(b"worktree ")
    ]
    for path in roots:
        if not path.is_absolute():
            raise Rejected("private-root-must-be-absolute")
        resolved = path.resolve()
        if any(resolved.is_relative_to(tree) or tree.is_relative_to(resolved) for tree in worktrees):
            raise Rejected("private-root-overlaps-worktree")


def artifact(profile, output, timing):
    # Only numeric measurements are copied into the upload report. The raw logs
    # and even the original timing file are kept on the disposable runner.
    report = {"schema_version": 1, "check": profile, "result": "passed"}
    if profile == "bootstrap":
        if timing is None or not stat.S_ISREG(timing.lstat().st_mode) or timing.stat().st_size > 128:
            raise Rejected("invalid-artifact-timings")
        match = re.fullmatch(rb"cold_seconds=([0-9]{1,8})\nwarm_seconds=([0-9]{1,8})\n", timing.read_bytes())
        if not match:
            raise Rejected("invalid-artifact-timings")
        report.update(cold_seconds=int(match[1]), warm_seconds=int(match[2]))
    elif timing is not None:
        raise Rejected("unexpected-artifact-input")
    check_private_roots([output])
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index")
    commands.add_parser("pre-push")
    history = commands.add_parser("range")
    history.add_argument("base")
    history.add_argument("head")
    history.add_argument("--pr-event", type=Path)
    private = commands.add_parser("private-root")
    private.add_argument("paths", nargs="+", type=Path)
    report = commands.add_parser("artifact")
    report.add_argument("profile", choices=["bootstrap", "supervisor"])
    report.add_argument("output", type=Path)
    report.add_argument("--timing", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "private-root":
            check_private_roots(args.paths)
        elif args.command == "artifact":
            artifact(args.profile, args.output, args.timing)
        elif args.command == "index":
            entries = index_entries()
            Guard(read_rules(entries)).check_tree(entries, require_policy=True)
        elif args.command == "pre-push":
            pre_push(sys.stdin)
        else:
            head = commit_oid(args.head)
            guard = Guard(read_rules(tree_entries(head)))
            if args.pr_event:
                guard.message(pr_metadata(args.pr_event), "pr-metadata")
            guard.range(head, commit_oid(args.base))
    except Rejected as error:
        location = f" at {json.dumps(error.location)}" if error.location else ""
        print(f"data-guard: {error.reason}{location}", file=sys.stderr)
        return 1
    except (KeyError, OSError, TypeError, ValueError, RuntimeError, re.error):
        print("data-guard: invalid or unavailable input", file=sys.stderr)
        return 1
    print(f"data-guard: {args.command} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
