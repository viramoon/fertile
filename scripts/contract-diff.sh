#!/usr/bin/env bash
# Check the two producers, both consumer inputs, provenance and regenerated clients.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
case ${1:-} in
  '') write=false ;;
  --write) write=true ;;
  *) echo 'Usage: scripts/contract-diff.sh [--write]' >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || exit 2
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/fertile/contract-target}
CARGO_TARGET_DIR=$(realpath -m "$CARGO_TARGET_DIR")

# The consumer working trees are also checked: a local edit to generated code is drift.
for repo in finance web frontend; do
  entry=$(git -C "$root" ls-files --stage -- "$repo")
  read -r mode pin stage path <<<"$entry"
  [[ $mode == 160000 && $stage == 0 && $path == "$repo" && $pin =~ ^[0-9a-f]{40}$ ]] || {
    echo "$repo: expected a single, resolved gitlink" >&2; exit 1; }
  [[ $(git -C "$root/$repo" rev-parse --verify HEAD) == "$pin" ]] || {
    echo "$repo: checkout differs from gitlink $pin" >&2; exit 1; }
  printf '%s\n' "$pin" > "$work/$repo.commit"
done
cmp "$work/finance.commit" "$root/web/openapi/finance.commit"
cmp "$work/web.commit" "$root/frontend/openapi/web.commit"

check_clients() {
  local snapshots=$1
  cmp "$snapshots/finance.openapi.json" "$root/web/openapi/finance.openapi.json"
  cmp "$snapshots/web.openapi.json" "$root/frontend/openapi/web.openapi.json"
  bash "$root/web/scripts/gen-finance-client.sh" --check "$snapshots/finance.openapi.json"
  bash "$root/frontend/scripts/gen-web-client.sh" --check "$snapshots/web.openapi.json"
}

if ! $write; then check_clients "$root/contracts"; fi
for repo in finance web; do
  pin=$(cat "$work/$repo.commit")
  mkdir "$work/$repo"
  git -C "$root/$repo" archive "$pin" | tar -x -C "$work/$repo"
  package=$repo
  [[ $repo != finance ]] || package=finance-api
  # An archive has no Git metadata. Give web/build.rs the exact producer revision.
  (cd "$work/$repo" && GITHUB_SHA="$pin" cargo run --quiet --locked --release -p "$package" -- export-openapi) > "$work/$repo.openapi.json"
done
if $write; then
  # Do not replace either snapshot until both producer exports and clients agree.
  check_clients "$work"
  for repo in finance web; do
    install -m 644 "$work/$repo.openapi.json" "$root/contracts/$repo.openapi.json"
  done
else
  for repo in finance web; do cmp "$work/$repo.openapi.json" "$root/contracts/$repo.openapi.json"; done
fi
printf 'Contract checks passed; finance=%s web=%s frontend=%s\n' \
  "$(cat "$work/finance.commit")" "$(cat "$work/web.commit")" "$(cat "$work/frontend.commit")"
