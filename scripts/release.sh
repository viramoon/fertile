#!/usr/bin/env bash
# Preview the release entry using committed Git history and GitHub's PR associations.
set -euo pipefail
if [[ $# != 1 || $1 != --dry-run ]]; then
  echo 'Usage: scripts/release.sh --dry-run' >&2
  exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec python3 scripts/release-preview.py
