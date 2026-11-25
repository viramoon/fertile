#!/usr/bin/env bash
# Operator drill: SIGKILL the finance-api owned by the selected fertile supervisor.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "${FERTILE_CONTROL_PYTHON:-python3}" "$here/supervisor_drill.py" chaos
