#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${FERTILE_ROOT:-${FERTILE_BOX_HOME:-/home/box}/fertile}
python=${FERTILE_CONTROL_PYTHON:-${FERTILE_SUPERVISOR_BIN:-$root/tools/supervisor/bin}/python}
exec "$python" "$base/network.py" "$@"
