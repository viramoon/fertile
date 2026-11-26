#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
source "$base/../../scripts/runtime-root.sh"
root=$(fertile_root)
python=${FERTILE_CONTROL_PYTHON:-${FERTILE_SUPERVISOR_BIN:-$root/tools/supervisor/bin}/python}
exec "$python" "$base/network.py" "$@"
