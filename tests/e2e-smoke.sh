#!/usr/bin/env bash
# Verify the selected deployment. Public mode requires an explicitly confirmed origin.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$here/e2e_smoke.py" "$@"
