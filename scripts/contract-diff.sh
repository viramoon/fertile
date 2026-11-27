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
CARGO_TARGET_DIR=$(realpath -m "$CARGO_TARGET_DIR/contract-exports")
mkdir -p "$CARGO_TARGET_DIR"
exec {export_lock}>"$CARGO_TARGET_DIR/.contract-diff.lock"
flock "$export_lock"
cargo_args=(--quiet --locked --release)
if [[ ${CARGO_NET_OFFLINE:-false} == true ]]; then cargo_args+=(--offline); fi

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

record_batch_one_web_baseline() {
  local recorded=$1 pin=$2
  local merged_w4=a6cd062393d18166bea2500d3873c37e564c3861
  # #44 batch 1 ships W2a while U5 still uses the pre-squash W4 contract.
  [[ $recorded == d9e5c79b492bdf4bf04b1f50987a78b515e7283b &&
     $pin == 9132c5dc2e40f94d96151fbcc2989abbbb53d778 &&
     $(cat "$work/frontend.commit") == 3788ae986ea61762b4219fd17547b5e2840c93cb ]] || return 1
  git -C "$root/web" merge-base --is-ancestor "$merged_w4" "$pin" || return 1
  printf '%s\n' "$recorded" > "$work/web-frontend.commit"
  printf '%s\n' "$merged_w4" > "$work/web-w4.commit"
}

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
  elif git -C "$root/$producer" merge-base --is-ancestor "$recorded" "$pin"; then
    if [[ $producer == finance ]] && ! git -C "$root/finance" diff --quiet "$recorded" "$pin" -- protocol/; then
      printf '%s: recorded finance ancestor and gitlink differ in versioned protocol files\n' "$marker" >&2
      return 1
    fi
    printf '%s\n' "$recorded" > "$work/$producer-recorded.commit"
  elif [[ $producer == web ]] && record_batch_one_web_baseline "$recorded" "$pin"; then
    printf 'web: #44 batch 1 checks the W4 frontend baseline and W2a deployment export independently\n'
  else
    printf '%s: recorded %s commit %s has a different tree from gitlink %s\n' "$marker" "$producer" "$recorded" "$pin" >&2
    return 1
  fi
}

check_producer_revision finance "$root/web/openapi/finance.commit"
check_producer_revision web "$root/frontend/openapi/web.commit"

check_clients() {
  local snapshots=$1
  local web_snapshot="$snapshots/web.openapi.json"
  if [[ -f $work/web-frontend.commit ]]; then
    # The recorded and merged W4 exports below authenticate this input.
    web_snapshot="$root/frontend/openapi/web.openapi.json"
  fi
  bash "$root/web/scripts/gen-finance-client.sh" --check "$snapshots/finance.openapi.json"
  bash "$root/frontend/scripts/gen-web-client.sh" --check "$web_snapshot"
}

export_contract() {
  local repo=$1 source=$2 pin package
  pin=$(cat "$work/$source.commit")
  mkdir "$work/$source"
  # Archived commit timestamps can be older than artifacts built from different sources.
  git -C "$root/$repo" archive "$pin" | tar -xm -C "$work/$source"
  package=$repo
  [[ $repo != finance ]] || package=finance-api
  # An archive has no Git metadata. Give web/build.rs the exact producer revision.
  (cd "$work/$source" && GIT_CEILING_DIRECTORIES="$work" GITHUB_SHA="$pin" cargo run "${cargo_args[@]}" -p "$package" -- export-openapi) > "$work/$source.openapi.json"
  if [[ $repo == finance ]]; then check_finance_stream "$source"; fi
}

check_finance_stream() {
  local source=$1 file
  (
    cd "$work/$source"
    cargo run "${cargo_args[@]}" -p finance-api -- export-read-stream "$work/$source-stream"
  )
  for file in market-ws.schema.json market-status-sse.schema.json read-stream-examples.json; do
    cmp "$work/$source-stream/$file" "$work/$source/protocol/v1/$file"
  done
  printf '%s: regenerated v1 read-stream schemas and examples match commit %s\n' \
    "$source" "$(cat "$work/$source.commit")"
}

if ! $write; then check_clients "$root/contracts"; fi
for repo in finance web; do
  export_contract "$repo" "$repo"
  if [[ -f $work/$repo-recorded.commit ]]; then
    export_contract "$repo" "$repo-recorded"
    if ! cmp -s "$work/$repo-recorded.openapi.json" "$work/$repo.openapi.json"; then
      printf '%s: recorded ancestor and gitlink export different OpenAPI bytes\n' "$repo" >&2
      exit 1
    fi
    printf '%s: recorded ancestor %s and gitlink %s export identical OpenAPI bytes\n' \
      "$repo" "$(cat "$work/$repo-recorded.commit")" "$(cat "$work/$repo.commit")"
  fi
done
cmp "$work/web.openapi.json" "$work/web/openapi/web.openapi.json"
if [[ -f $work/web-frontend.commit ]]; then
  export_contract web web-frontend
  export_contract web web-w4
  cmp "$work/web-frontend.openapi.json" "$work/web-w4.openapi.json"
  cmp "$work/web-frontend.openapi.json" "$root/frontend/openapi/web.openapi.json"
  printf 'web: frontend snapshot matches both its recorded export and the merged W4 export; U5 pending\n'
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
