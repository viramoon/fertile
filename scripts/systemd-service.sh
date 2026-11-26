#!/usr/bin/env bash
# The unit executes this launcher as its configured service account.
set -euo pipefail
(( EUID != 0 )) || { echo 'Run Fertile services and status as the service account.' >&2; exit 1; }
scripts=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${FERTILE_ROOT:?FERTILE_ROOT is required for systemd services}
# shellcheck disable=SC1091
source "$root/env.sh"
export FERTILE_ROOT="$root" PYTHONDONTWRITEBYTECODE=1
unset FERTILE_SERVICE_MANIFEST
exec "$root/tools/supervisor/bin/python" "$scripts/systemd_runtime.py" "$@"
