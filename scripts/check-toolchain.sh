#!/usr/bin/env bash
set -Eeuo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$repo/deploy/toolchain.env"
source "${FERTILE_BOX_HOME:-/home/box}/fertile/env.sh"
[[ "$(rustc --version)" = "rustc $RUST_VERSION "* ]]
[[ "$(python --version)" = "Python $PYTHON_VERSION" ]]
[[ "$(uv --version)" = "uv $UV_VERSION"* ]]
[[ "$(node --version)" = "v$NODE_VERSION" ]]
[[ "$(pnpm --version)" = "$PNPM_VERSION" ]]
[[ "$(clang --version)" = *"clang version $LLVM_VERSION"* ]]
[[ "$(ld.lld --version)" = *"$LLVM_VERSION"* ]]
[[ "$(pg_ctl --version)" = "pg_ctl (PostgreSQL) $POSTGRES_VERSION" ]]
for command in rustc cargo python uv node pnpm clang ld.lld pg_ctl psql initdb; do
  path=$(command -v "$command")
  [[ "$path" = "$FERTILE_ROOT/"* ]] || { echo "工具越出持久根：$command $path" >&2; exit 1; }
done
echo "PASS locked toolchain in $FERTILE_ROOT"
