#!/usr/bin/env bash
# Check the two producers, both consumer inputs, provenance and regenerated clients.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
case ${1:-} in
  ''|--check) write=false ;;
  --write) write=true ;;
  *) echo 'Usage: scripts/contract-diff.sh [--check|--write]' >&2; exit 2 ;;
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

check_producer_revision() {
  local producer=$1 marker=$2 recorded pin recorded_tree pin_tree
  if cmp -s "$work/$producer.commit" "$marker"; then return; fi
  if ! IFS= read -r recorded < "$marker" || [[ ! $recorded =~ ^[0-9a-f]{40}$ ]] ||
    ! printf '%s\n' "$recorded" | cmp -s - "$marker"; then
    printf '%s: expected one full producer commit SHA\n' "$marker" >&2
    return 1
  fi
  if [[ $(git -C "$root/$producer" rev-parse --verify --quiet --end-of-options "$recorded^{commit}") != "$recorded" ]]; then
    printf '%s: recorded %s commit %s is unavailable\n' "$marker" "$producer" "$recorded" >&2
    return 1
  fi
  # A squash merge can record a new commit for the exact same producer source.
  pin=$(cat "$work/$producer.commit")
  recorded_tree=$(git -C "$root/$producer" rev-parse --verify "$recorded^{tree}")
  pin_tree=$(git -C "$root/$producer" rev-parse --verify "$pin^{tree}")
  if [[ $recorded_tree == "$pin_tree" ]]; then
    printf '%s: recorded %s and gitlink %s share tree %s\n' "$producer" "$recorded" "$pin" "$pin_tree"
  elif [[ $producer == web ]] && git -C "$root/$producer" merge-base --is-ancestor "$recorded" "$pin"; then
    # Static-only web upgrades can keep the client derived from an ancestor.
    printf '%s\n' "$recorded" > "$work/web-recorded.commit"
  else
    printf '%s: recorded %s commit %s has a different tree from gitlink %s\n' "$marker" "$producer" "$recorded" "$pin" >&2
    return 1
  fi
}

check_producer_revision finance "$root/web/openapi/finance.commit"
check_producer_revision web "$root/frontend/openapi/web.commit"

check_clients() {
  local snapshots=$1
  cmp "$snapshots/finance.openapi.json" "$root/web/openapi/finance.openapi.json"
  cmp "$snapshots/web.openapi.json" "$root/frontend/openapi/web.openapi.json"
  bash "$root/web/scripts/gen-finance-client.sh" --check "$snapshots/finance.openapi.json"
  bash "$root/frontend/scripts/gen-web-client.sh" --check "$snapshots/web.openapi.json"
}

export_contract() {
  local repo=$1 source=$2 pin package
  pin=$(cat "$work/$source.commit")
  mkdir "$work/$source"
  git -C "$root/$repo" archive "$pin" | tar -x -C "$work/$source"
  package=$repo
  [[ $repo != finance ]] || package=finance-api
  # An archive has no Git metadata. Give web/build.rs the exact producer revision.
  (cd "$work/$source" && GIT_CEILING_DIRECTORIES="$work" GITHUB_SHA="$pin" cargo run --quiet --locked --release -p "$package" -- export-openapi) > "$work/$source.openapi.json"
}

if ! $write; then check_clients "$root/contracts"; fi
for repo in finance web; do
  export_contract "$repo" "$repo"
done
if [[ -f $work/web-recorded.commit ]]; then
  export_contract web web-recorded
  if ! cmp -s "$work/web-recorded.openapi.json" "$work/web.openapi.json"; then
    echo 'frontend/openapi/web.commit: recorded web ancestor and gitlink export different OpenAPI bytes' >&2
    exit 1
  fi
  printf 'web: recorded ancestor %s and gitlink %s export identical OpenAPI bytes\n' \
    "$(cat "$work/web-recorded.commit")" "$(cat "$work/web.commit")"
fi
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
