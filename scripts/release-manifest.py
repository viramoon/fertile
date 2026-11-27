#!/usr/bin/env python3
"""Record and verify the locally built deployment; no release tag is created."""
import hashlib
import json
import os
import subprocess
from importlib import metadata
from pathlib import Path
import sys
from urllib.parse import urlparse, unquote


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def installed_wheel():
    direct = json.loads(metadata.distribution("nautilus_trader").read_text("direct_url.json"))
    uri = urlparse(direct["url"])
    if uri.scheme != "file" or uri.netloc:
        raise ValueError("Nautilus must be installed from the local source wheel")
    return Path(unquote(uri.path)), direct.get("archive_info", {}).get("hashes", {}).get("sha256")


def wheel_for(commit):
    wheel, installed_hash = installed_wheel()
    expected, filename = (wheel.parent / "SHA256SUMS").read_text().strip().split(maxsplit=1)
    if (wheel.parent / "source-commit").read_text().strip() != commit:
        raise ValueError("wheel source does not match the locked gitlink")
    if filename.removeprefix("./") != wheel.name or digest(wheel) != expected or (installed_hash and installed_hash != expected):
        raise ValueError("wheel filename or SHA256 differs from the source build")
    return wheel


def tree_digest(directory):
    result = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        # A Python invocation may compile tracked modules into a local bytecode cache.
        if "__pycache__" in path.relative_to(directory).parts:
            continue
        if path.is_symlink():
            raise ValueError(f"unexpected symbolic link in deployment assets: {path}")
        if path.is_file():
            result.update(os.fsencode(path.relative_to(directory)) + b"\0")
            result.update(bytes.fromhex(digest(path)))
    return result.hexdigest()


def main():
    command, *args = sys.argv[1:]
    if command == "optional-bins":
        component, source = args
        allowed = {
            "finance": {"finance-binance", "finance-decision", "finance-market-wal", "finance-strategy"},
            "web": {"web-notifier"},
        }[component]
        packages = json.loads(Path(source).read_text())["packages"]
        built = {target["name"] for package in packages for target in package["targets"]
                 if "bin" in target["kind"] and not target.get("required-features")}
        for name in sorted(allowed & built):
            print(name)
        return
    if command == "distribution":
        wheel, sha = installed_wheel()
        print(json.dumps({"wheel": wheel.name, "sha256": sha}))
        return
    if command == "wheel":
        print(wheel_for(args[0]))
        return
    directory, ref, finance, web, frontend, *extra = args
    root = Path(directory)
    for name in ("bin/finance-api", "bin/finance-collector", "bin/finance-ib", "bin/web",
                 "finance/scripts/finance-backtest-worker", "runtime/venv/bin/python"):
        if not os.access(root / name, os.X_OK):
            raise ValueError(f"missing deployment executable: {name}")
    for name in ("static/index.html", "web/config/base.yaml", "finance/sql/backtest-queue.sql",
                 "finance/pyproject.toml", "finance/vendor/nautilus_trader/pyproject.toml",
                 "finance/vendor/nautilus_trader/examples/backtest/example_01_load_bars_from_custom_csv/run_example.py"):
        if not (root / name).is_file():
            raise ValueError(f"missing deployment file: {name}")
    common = {
        "format": 1,
        "fertile": ref,
        "components": {"finance": finance, "web": web, "frontend": frontend},
        "sha256": {part: tree_digest(root / part) for part in ("bin", "static", "web", "finance", "fertile")},
    }
    manifest = root / "manifest.json"
    if command == "create":
        commit, wheel = extra
        value = {**common, "nautilus": {"commit": commit, "wheel": Path(wheel).name, "sha256": digest(Path(wheel))}}
        manifest.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    elif command == "verify":
        recorded = json.loads(manifest.read_text())
        if any(recorded.get(key) != value for key, value in common.items()):
            raise ValueError("installed payload or its locked revisions differ from manifest.json")
        python = root / "runtime/venv/bin/python"
        installed = json.loads(subprocess.check_output([str(python), __file__, "distribution"]))
        if installed["wheel"] != recorded["nautilus"]["wheel"] or (installed["sha256"] and installed["sha256"] != recorded["nautilus"]["sha256"]):
            raise ValueError("installed Nautilus distribution differs from the recorded wheel")
        subprocess.run([str(python), str(root / "finance/scripts/verify-source.py")],
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=True)
        source = dict(line.split("=", 1) for line in (root / "finance/toolchain.env").read_text().splitlines()
                      if line and not line.startswith("#"))
        if recorded["nautilus"]["commit"] != source["NAUTILUS_COMMIT"]:
            raise ValueError("Nautilus manifest commit differs from finance toolchain")
    else:
        raise ValueError(f"unknown manifest operation: {command}")


if __name__ == "__main__":
    main()
