#!/usr/bin/env bash
# Test the F12 source committed by the fertile candidate.
set -euo pipefail
cd -- "$(git rev-parse --show-toplevel)"
entry=$(git ls-tree HEAD -- finance)
read -r mode kind pin path <<< "$entry"
if [[ $mode != 160000 || $kind != commit || $path != finance ]]; then
  echo 'F12 requires a committed finance gitlink' >&2
  exit 1
fi
if [[ ! -e finance/.git ]] || [[ $(git -C finance rev-parse --verify HEAD) != "$pin" ]]; then
  printf 'F12 finance checkout must match committed gitlink %s\n' "$pin" >&2
  exit 1
fi
printf 'F12 testing finance gitlink %s\n' "$pin"

producer=$(mktemp -d "${RUNNER_TEMP:-/tmp}/finance-f12.XXXXXXXX")
trap 'rm -rf -- "$producer"' EXIT
git -C finance archive "$pin" | tar -xm -C "$producer"
cd -- "$producer"
# Revisions of the same Cargo packages must not share compiled artifacts.
export CARGO_TARGET_DIR="$producer/target" CARGO_NET_OFFLINE=true
cargo test --offline --locked --release -p finance-collector --test option_history
# The standalone finance-ib build needs the Chrono OpenAPI support that
# finance-collector otherwise enables when it is selected as a test target.
cargo test --offline --locked --release -p finance-ib --lib --features utoipa/chrono \
  verified_paper_option_history_requests_and_decodes_synthetic_bars
