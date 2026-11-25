#!/usr/bin/env bash
# Exercise the real checkout/failure path in a private root without a network or installed services.
set -Eeuo pipefail
builder=$(cd -- "$(dirname -- "$0")/../scripts" && pwd)/build-release.sh
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
repo="$tmp/source"
root="$tmp/runtime"
mkdir -p "$root/releases/previous"
ln -s releases/previous "$root/current"
export FERTILE_SOURCE_DIR=$repo FERTILE_ROOT=$root
export GIT_AUTHOR_NAME=test GIT_COMMITTER_NAME=test
export GIT_AUTHOR_EMAIL=test@example.invalid GIT_COMMITTER_EMAIL=test@example.invalid
git init -q "$repo"
for component in finance web frontend; do
  component_repo="$repo/$component"
  git init -q "$component_repo"
  # Missing the nested Nautilus gitlink causes a controlled failure after all three checkouts.
  echo initial > "$component_repo/file"
  git -C "$component_repo" add file
  git -C "$component_repo" commit -qm initial
  revision=$(git -C "$component_repo" rev-parse HEAD)
  git -C "$repo" config --file .gitmodules "submodule.$component.path" "$component"
  git -C "$repo" config --file .gitmodules "submodule.$component.url" "$component_repo"
  git -C "$repo" update-index --add --cacheinfo "160000,$revision,$component"
  echo 'uncommitted user data' > "$component_repo/file"
done
git -C "$repo" add .gitmodules
git -C "$repo" commit -qm fixture
for id in 1 2; do
  "$builder" >"$tmp/build-$id.log" 2>&1 &
  pids[id]=$!
done
for id in 1 2; do
  if wait "${pids[$id]}"; then echo 'Incomplete component unexpectedly activated' >&2; exit 1; fi
  grep -Fq 'missing gitlink: vendor/nautilus_trader' "$tmp/build-$id.log"
done
[[ $(readlink "$root/current") == releases/previous ]]
[[ $(find "$root/releases" -mindepth 1 -maxdepth 1 | wc -l) -eq 1 ]]
[[ -z $(find "$root/cache/build" -mindepth 1 -maxdepth 1 -name '.work-*') ]]
for component in finance web frontend; do grep -Fxq 'uncommitted user data' "$repo/$component/file"; done
echo 'PASS two failed builds preserved current, cleaned staging and left all three dirty checkouts untouched'
