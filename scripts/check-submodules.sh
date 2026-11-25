#!/usr/bin/env bash
# Verify the committed deployment pins against the checkout, the index and remote history.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
branch=main
if [[ $# == 2 && $1 == --ref ]]; then branch=$2
elif [[ $# != 0 ]]; then echo 'Usage: scripts/check-submodules.sh [--ref branch]' >&2; exit 2
fi
git check-ref-format "refs/heads/$branch"
components=(finance frontend web)
fail() { echo "$*" >&2; exit 1; }
expected=$(printf '%s\n' "${components[@]}")
registered=$(git config --file .gitmodules --get-regexp '^submodule\..*\.path$' | cut -d' ' -f2- | LC_ALL=C sort)
[[ $registered == "$expected" ]] || fail 'expected exactly finance, frontend, web in .gitmodules'
pins=$(git ls-tree HEAD | awk '$1=="160000" {print $4}' | LC_ALL=C sort)
[[ $pins == "$expected" ]] || fail 'unexpected committed gitlinks'
git diff --quiet HEAD -- .gitmodules || fail 'uncommitted .gitmodules'
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
for component in "${components[@]}"; do
  url="https://github.com/viramoon/$component.git"
  [[ $(git config --file .gitmodules --get "submodule.$component.url") == "$url" ]] || fail "wrong URL: $component"
  [[ $(git config --file .gitmodules --get "submodule.$component.path") == "$component" ]] || fail "wrong path: $component"
  [[ $(git config --file .gitmodules --get "submodule.$component.branch") == main ]] || fail "wrong tracking branch: $component"
  sha=$(git rev-parse "HEAD:$component")
  [[ $(git rev-parse ":$component") == "$sha" && -e $component/.git ]] || fail "uninitialized or staged gitlink change: $component"
  [[ $(git -C "$component" rev-parse HEAD) == "$sha" ]] || fail "checkout mismatch: $component"
  [[ -z $(git -C "$component" status --porcelain=v1 --untracked-files=all --ignore-submodules=none) ]] || fail "dirty checkout: $component"
  nested=$(git -C "$component" submodule status --recursive)
  if [[ -n $nested ]] && printf '%s\n' "$nested" | grep -Eq '^[^ ]'; then fail "nested checkout mismatch: $component"; fi
  upstream=$(git ls-remote --exit-code "$url" "refs/heads/$branch")
  tip=${upstream%%$'\t'*}
  if [[ $tip != "$sha" ]]; then
    git init -q --bare "$scratch/$component"
    git -C "$scratch/$component" fetch --quiet --no-tags "$url" "refs/heads/$branch"
    git -C "$scratch/$component" merge-base --is-ancestor "$sha" FETCH_HEAD || fail "$component@$sha is outside $branch history"
    tip=$(git -C "$scratch/$component" rev-parse FETCH_HEAD)
  fi
  printf '%s gitlink=%s remote=%s tip=%s clean\n' "$component" "$sha" "$branch" "$tip"
done
