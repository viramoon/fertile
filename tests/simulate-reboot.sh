#!/usr/bin/env bash
# Operator drill: interrupt the selected supervisor and its service tree, then ensure.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "${FERTILE_CONTROL_PYTHON:-python3}" "$here/supervisor_drill.py" reboot
