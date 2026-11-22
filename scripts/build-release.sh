#!/usr/bin/env bash
# Build exactly the committed component revisions, then activate the complete directory.
set -Eeuo pipefail
umask 027
trap 'echo "Build failed at ${BASH_SOURCE[0]}:$LINENO" >&2' ERR
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(git -C "${FERTILE_SOURCE_DIR:-$script_dir/..}" rev-parse --show-toplevel)
ref=HEAD
case $# in
  0) ;;
  2) [[ $1 == --ref && -n $2 ]] || { echo 'usage: fertile build [--ref <commit>]' >&2; exit 2; }; ref=$2 ;;
  *) echo 'usage: fertile build [--ref <commit>]' >&2; exit 2 ;;
esac

root=${FERTILE_ROOT:-${FERTILE_BOX_HOME:-/home/box}/fertile}
[[ $root == /* && $root != / ]] || { echo 'FERTILE_ROOT must be an absolute installation directory' >&2; exit 2; }
if [[ -f $root/env.sh ]]; then
  # shellcheck source=/dev/null
  source "$root/env.sh"
fi
export FERTILE_ROOT=$root
mkdir -p "$root"/{run,releases,cache/build}
exec 9>"$root/run/control.lock"
flock -x 9
sha=$(git -C "$repo" rev-parse --verify --end-of-options "$ref^{commit}")
release="$root/releases/$sha"
[[ ! -e $root/current || -L $root/current ]] || { echo 'current must be a symbolic link' >&2; exit 1; }
[[ ! -L $release ]] || { echo 'release cannot be a symbolic link' >&2; exit 1; }

gitlink() {
  local entry mode kind revision path
  entry=$(git -C "$1" ls-tree "$2" -- "$3")
  read -r mode kind revision path <<<"$entry"
  [[ $mode == 160000 && $kind == commit && $path == "$3" ]] || { echo "missing gitlink: $3" >&2; return 1; }
  printf '%s\n' "$revision"
}

components=(finance web frontend)
declare -A revisions
for component in "${components[@]}"; do
  revisions[$component]=$(gitlink "$repo" "$sha" "$component")
done
manifest_args=("$sha" "${revisions[finance]}" "${revisions[web]}" "${revisions[frontend]}")
scratch=$(mktemp -d "$root/cache/build/.work-XXXXXXXX")
stage=
switch=
cleanup() {
  rm -rf -- "$scratch"
  if [[ -n $stage ]]; then rm -rf -- "$stage"; fi
  if [[ -n $switch ]]; then rm -rf -- "$switch"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

checkout_at() {
  local target=$1 hint=$2 revision=$3 remote=$4
  local source=$remote
  # The caller's checkouts may be dirty, on another branch or uninitialized.
  # Read objects from them when available; only our own scratch repository is checked out.
  if [[ -e $hint/.git ]] && git -C "$hint" cat-file -e "$revision^{commit}" 2>/dev/null; then source=$hint; fi
  git init -q "$target"
  git -C "$target" fetch -q --no-tags --depth=1 "$source" "$revision"
  git -C "$target" checkout -q --detach "$revision"
  [[ $(git -C "$target" rev-parse HEAD) == "$revision" ]]
}

build_payload() {
  local output=$1 component url finance vendor vendor_sha vendor_cache vendor_tmp target example wheel
  for component in "${components[@]}"; do
    url=$(git -C "$repo" config --blob "$sha:.gitmodules" --get "submodule.$component.url")
    checkout_at "$scratch/$component" "$repo/$component" "${revisions[$component]}" "$url"
  done
  finance="$scratch/finance"; vendor=vendor/nautilus_trader
  vendor_sha=$(gitlink "$finance" HEAD "$vendor")
  vendor_cache="$root/cache/build/nautilus/$vendor_sha"
  if [[ ! -d $vendor_cache ]]; then
    mkdir -p "$(dirname "$vendor_cache")"
    vendor_tmp="$scratch/nautilus"
    url=$(git -C "$finance" config --get --file .gitmodules "submodule.$vendor.url")
    checkout_at "$vendor_tmp" "$repo/finance/$vendor" "$vendor_sha" "$url"
    mv -T -- "$vendor_tmp" "$vendor_cache"
  fi
  [[ $(git -C "$vendor_cache" rev-parse HEAD) == "$vendor_sha" ]]
  rmdir "$finance/$vendor"
  ln -s -- "$vendor_cache" "$finance/$vendor"

  mkdir -p "$output"/{bin,static,web,finance,runtime,fertile}
  git -C "$repo" archive "$sha" scripts deploy | tar -x -C "$output/fertile"
  # shellcheck source=deploy/toolchain.env
  source "$output/fertile/deploy/toolchain.env"
  [[ $vendor_sha == "$NAUTILUS_COMMIT" && $(node --version) == "v$NODE_VERSION" && $(pnpm --version) == "$PNPM_VERSION" ]]
  # The finance build checks its lock, upstream commit, wheel checksum and native extension.
  export FINANCE_BUILD_ROOT=${FINANCE_BUILD_ROOT:-$root/cache/finance}
  bash "$finance/scripts/build.sh" | tee "$scratch/finance-build.log"
  wheel=$("$FINANCE_BUILD_ROOT/venv/bin/python" "$script_dir/release-manifest.py" wheel "$vendor_sha")
  # The release gets its own relocatable environment, populated from uv's content-addressed
  # cache, including the exact local wheel. No venv is shared between running releases.
  (
    export UV_PROJECT_ENVIRONMENT="$output/runtime/venv" UV_CACHE_DIR="$FINANCE_BUILD_ROOT/uv-cache"
    export UV_LINK_MODE=hardlink UV_VENV_RELOCATABLE=true
    cd "$finance"
    uv venv --relocatable --python "$PYTHON_VERSION" "$UV_PROJECT_ENVIRONMENT"
    uv sync --frozen --no-default-groups --no-install-package nautilus-trader --python "$PYTHON_VERSION"
    uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" --no-deps "$wheel"
  )

  target=${CARGO_TARGET_DIR:-$root/cache/cargo-target}
  [[ $target == /* ]] || { echo 'CARGO_TARGET_DIR must be absolute' >&2; return 1; }
  for component in finance web; do
    (cd "$scratch/$component" && CARGO_INCREMENTAL=0 SQLX_OFFLINE=true cargo build --release --locked --bins --target-dir "$target")
  done
  for component in finance-api finance-ib finance-collector web; do install -m 0755 "$target/release/$component" "$output/bin/$component"; done
  (cd "$scratch/frontend" && pnpm install --frozen-lockfile && pnpm run build)
  cp -a "$scratch/frontend/dist/." "$output/static/"
  cp -a "$scratch/web/config" "$output/web/"
  git -C "$finance" archive HEAD config scripts python sql pyproject.toml toolchain.env | tar -x -C "$output/finance"
  example=examples/backtest/example_01_load_bars_from_custom_csv
  mkdir -p "$output/finance/$vendor"
  git -C "$vendor_cache" archive "$vendor_sha" pyproject.toml "$example" | tar -x -C "$output/finance/$vendor"
  PYTHONDONTWRITEBYTECODE=1 "$output/runtime/venv/bin/python" "$output/finance/scripts/verify-source.py"
  python3 "$script_dir/release-manifest.py" create "$output" "${manifest_args[@]}" "$vendor_sha" "$wheel"
}

if [[ -d $release ]]; then
  python3 "$script_dir/release-manifest.py" verify "$release" "${manifest_args[@]}"
  echo "Reusing verified release $sha"
else
  stage=$(mktemp -d "$root/releases/.build-XXXXXXXX")
  build_payload "$stage"
  python3 "$script_dir/release-manifest.py" verify "$stage" "${manifest_args[@]}"
  mv -T -- "$stage" "$release"
  stage=
fi
switch=$(mktemp -d "$root/.switch-XXXXXXXX")
ln -s -- "releases/$sha" "$switch/current"
mv -Tf -- "$switch/current" "$root/current"
echo "Activated $release"
