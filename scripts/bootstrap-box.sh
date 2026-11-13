#!/usr/bin/env bash
# 在持久前缀中安装固定版本；系统包仅作为可重建的编译/运行依赖。
set -Eeuo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$repo/deploy/toolchain.env"
box=${FERTILE_BOX_HOME:-/home/box}
[[ "$box" = /* && "$box" != / && "$box" != *[!a-zA-Z0-9_./-]* ]] ||
  { echo "FERTILE_BOX_HOME 必须是无空格的绝对持久路径，不能为 /" >&2; exit 2; }
[[ "$(uname -s)" = Linux && "$(uname -m)" = x86_64 ]] ||
  { echo "当前安装器仅支持 Linux x86_64" >&2; exit 2; }
root="$box/fertile"
tools="$root/tools"
cache="$root/cache"
bin="$root/bin"
mkdir -p "$tools" "$cache/downloads" "$bin" "$root/logs" "$root/run" "$root/data"
trap 'code=$?; echo "初始化失败（退出码 $code）。保留缓存；修复日志中的问题后重新执行 scripts/bootstrap-box.sh。" >&2; exit "$code"' ERR

log() { printf '%s\n' "$*"; }
download() {
  local url=$1 path=$2
  if [[ -s "$path" ]]; then return; fi
  curl --fail --location --retry 5 --retry-all-errors --retry-delay 2 \
    --connect-timeout 20 --max-time 1800 "$url" -o "$path.part"
  mv -- "$path.part" "$path"
}
system_deps() {
  local packages=(build-essential ca-certificates curl xz-utils bzip2 git pkg-config
    libssl-dev zlib1g-dev libtinfo6 libxml2 libz3-4 bison flex)
  local missing=() p
  for p in "${packages[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q '^install ok installed$' ||
      missing+=("$p")
  done
  if (( ${#missing[@]} == 0 )); then log "SKIP system-deps"; return; fi
  local apt=(apt-get)
  if (( EUID != 0 )); then
    command -v sudo >/dev/null || { echo "缺少系统依赖：${missing[*]}；请由管理员安装，或在有 sudo 的盒子运行。" >&2; return 1; }
    apt=(sudo -n --preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy,SSL_CERT_FILE apt-get)
  fi
  "${apt[@]}" -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
  "${apt[@]}" -o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends "${missing[@]}"
}
system_deps

export RUSTUP_HOME="$tools/rustup" CARGO_HOME="$tools/cargo" RUSTUP_TOOLCHAIN="$RUST_VERSION"
if [[ -x "$CARGO_HOME/bin/rustc" ]] &&
   [[ "$("$CARGO_HOME/bin/rustc" --version)" = "rustc $RUST_VERSION "* ]]; then
  log "SKIP rust $RUST_VERSION"
else
  log "INSTALL rust $RUST_VERSION"
  mkdir -p "$cache/downloads/rustup/$RUSTUP_VERSION"
  download "https://static.rust-lang.org/rustup/archive/$RUSTUP_VERSION/x86_64-unknown-linux-gnu/rustup-init" "$cache/downloads/rustup/$RUSTUP_VERSION/rustup-init"
  chmod +x "$cache/downloads/rustup/$RUSTUP_VERSION/rustup-init"
  "$cache/downloads/rustup/$RUSTUP_VERSION/rustup-init" -y --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION"
fi

uv_dir="$tools/uv-$UV_VERSION"
if [[ -x "$uv_dir/uv" ]] && [[ "$("$uv_dir/uv" --version)" = "uv $UV_VERSION"* ]]; then
  log "SKIP uv $UV_VERSION"
else
  log "INSTALL uv $UV_VERSION"
  download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" "$cache/downloads/uv-$UV_VERSION.tar.gz"
  mkdir -p "$uv_dir"
  tar -xzf "$cache/downloads/uv-$UV_VERSION.tar.gz" --strip-components=1 -C "$uv_dir"
fi
ln -sfn "$uv_dir/uv" "$bin/uv"
export UV_PYTHON_INSTALL_DIR="$tools/python" UV_CACHE_DIR="$cache/uv"
python_path=$("$bin/uv" python find --managed-python "$PYTHON_VERSION" 2>/dev/null || true)
if [[ -n "$python_path" ]] && [[ "$("$python_path" --version)" = "Python $PYTHON_VERSION" ]]; then
  log "SKIP python $PYTHON_VERSION"
else
  log "INSTALL python $PYTHON_VERSION"
  "$bin/uv" python install "$PYTHON_VERSION"
  python_path=$("$bin/uv" python find --managed-python "$PYTHON_VERSION")
fi
ln -sfn "$python_path" "$bin/python"
ln -sfn "$python_path" "$bin/python3"

node_dir="$tools/node-$NODE_VERSION"
if [[ -x "$node_dir/bin/node" ]] && [[ "$("$node_dir/bin/node" --version)" = "v$NODE_VERSION" ]]; then
  log "SKIP node $NODE_VERSION"
else
  log "INSTALL node $NODE_VERSION"
  node_archive="node-v$NODE_VERSION-linux-x64.tar.xz"
  download "https://nodejs.org/dist/v$NODE_VERSION/$node_archive" "$cache/downloads/$node_archive"
  download "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt" "$cache/downloads/node-$NODE_VERSION-SHASUMS256.txt"
  (cd "$cache/downloads"; grep " $node_archive\$" "node-$NODE_VERSION-SHASUMS256.txt" | sha256sum --check -)
  mkdir -p "$node_dir"
  tar -xJf "$cache/downloads/$node_archive" --strip-components=1 -C "$node_dir"
fi
pnpm_dir="$tools/pnpm-$PNPM_VERSION"
if [[ -x "$pnpm_dir/pnpm" ]] && [[ "$("$pnpm_dir/pnpm" --version)" = "$PNPM_VERSION" ]]; then
  log "SKIP pnpm $PNPM_VERSION"
else
  log "INSTALL pnpm $PNPM_VERSION"
  download "https://registry.npmjs.org/@pnpm/exe.linux-x64/-/exe.linux-x64-$PNPM_VERSION.tgz" "$cache/downloads/pnpm-native-$PNPM_VERSION.tgz"
  mkdir -p "$pnpm_dir"
  tar -xzf "$cache/downloads/pnpm-native-$PNPM_VERSION.tgz" --strip-components=1 -C "$pnpm_dir"
  chmod +x "$pnpm_dir/pnpm"
fi
ln -sfn "$pnpm_dir/pnpm" "$bin/pnpm"

llvm_dir="$tools/llvm-$LLVM_VERSION"
if [[ -x "$llvm_dir/bin/clang" ]] &&
   [[ "$(LD_LIBRARY_PATH="$llvm_dir/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$llvm_dir/bin/clang" --version)" = *"clang version $LLVM_VERSION"* ]]; then
  log "SKIP llvm $LLVM_VERSION"
else
  log "INSTALL llvm $LLVM_VERSION"
  llvm_archive="LLVM-$LLVM_VERSION-Linux-X64.tar.xz"
  download "https://github.com/llvm/llvm-project/releases/download/llvmorg-$LLVM_VERSION/$llvm_archive" "$cache/downloads/llvm-$LLVM_VERSION.tar.xz"
  mkdir -p "$llvm_dir"
  # 只展开编译、链接、libclang 与资源文件，避免持久化无关工具和静态库。
  tar -xJf "$cache/downloads/llvm-$LLVM_VERSION.tar.xz" --strip-components=1 -C "$llvm_dir" \
    --wildcards '*/bin/clang' '*/bin/clang++' "*/bin/clang-${LLVM_VERSION%%.*}" '*/bin/lld' '*/bin/ld.lld' \
    '*/lib/lib*.so*' '*/lib/clang/*' '*/include/clang-c/*'
fi

pg_dir="$tools/postgresql-$POSTGRES_VERSION"
# env.sh 不包含密钥；source 时保留调用者的 PATH 和库目录。
env_tmp=$(mktemp "$root/.env.XXXXXX")
{
  printf '# 由 scripts/bootstrap-box.sh 生成；重新运行可重建。无密钥。\n'
  printf 'export FERTILE_BOX_HOME=%q\nexport FERTILE_ROOT=%q\n' "$box" "$root"
  for name in RUSTUP_HOME CARGO_HOME RUSTUP_TOOLCHAIN UV_PYTHON_INSTALL_DIR UV_CACHE_DIR; do
    printf 'export %s=%q\n' "$name" "${!name}"
  done
  printf 'export CARGO_TARGET_DIR=%q\nexport LIBCLANG_PATH=%q\n' "$cache/cargo-target" "$llvm_dir/lib"
  printf 'export PNPM_HOME=%q\nexport npm_config_cache=%q\n' "$tools/pnpm-home" "$cache/npm"
  printf 'export UV_PYTHON_PREFERENCE=only-managed\n'
  printf 'export PATH=%q:"$PATH"\n' "$bin:$CARGO_HOME/bin:$node_dir/bin:$llvm_dir/bin:$pg_dir/bin"
  printf 'export LD_LIBRARY_PATH=%q"${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n' "$llvm_dir/lib:$(dirname "$(dirname "$python_path")")/lib"
} > "$env_tmp"
if cmp -s "$env_tmp" "$root/env.sh"; then rm "$env_tmp"; else mv "$env_tmp" "$root/env.sh"; fi
source "$root/env.sh"
if [[ -x "$pg_dir/bin/pg_ctl" ]] && [[ "$("$pg_dir/bin/pg_ctl" --version)" = "pg_ctl (PostgreSQL) $POSTGRES_VERSION" ]]; then
  log "SKIP postgres $POSTGRES_VERSION"
else
  log "INSTALL postgres $POSTGRES_VERSION"
  download "https://ftp.postgresql.org/pub/source/v$POSTGRES_VERSION/postgresql-$POSTGRES_VERSION.tar.bz2" "$cache/downloads/postgresql-$POSTGRES_VERSION.tar.bz2"
  mkdir -p "$cache/sources"
  if [[ ! -d "$cache/sources/postgresql-$POSTGRES_VERSION" ]]; then
    tar -xjf "$cache/downloads/postgresql-$POSTGRES_VERSION.tar.bz2" -C "$cache/sources"
  fi
  (
    cd "$cache/sources/postgresql-$POSTGRES_VERSION"
    ./configure --prefix="$pg_dir" --with-openssl --without-icu --without-readline
    make -j"${FERTILE_BUILD_JOBS:-2}"
    make install
  )
fi
"$repo/scripts/check-toolchain.sh"
log "READY source $root/env.sh"
