#!/usr/bin/env bash
set -euo pipefail
scripts=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
source "$scripts/../deploy/toolchain.env"
export IB_GATEWAY_VERSION IBC_VERSION
exec python3 "$scripts/ibgateway.py" "$@"
