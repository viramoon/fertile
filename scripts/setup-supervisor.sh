#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$repo/scripts/runtime-root.sh"
root=$(fertile_root)
venv="$root/tools/supervisor"
lock="$repo/deploy/supervisor-requirements.txt"
if [[ -x "$venv/bin/python" ]] && "$venv/bin/python" - "$lock" <<'PY'
from importlib import metadata
from pathlib import Path
import sys
raise SystemExit(any(metadata.version(name) != version for name, version in
                     (s.split("==") for s in Path(sys.argv[1]).read_text().splitlines() if s)))
PY
then exit 0; fi
mkdir -p "$root/tools" "$root/run"
exec 8>"$root/run/supervisor-install.lock"
flock -x 8
if [[ ! -x "$venv/bin/python" ]]; then uv venv --python python3 "$venv"; fi
uv pip install --python "$venv/bin/python" --requirement "$lock"
