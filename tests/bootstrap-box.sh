#!/usr/bin/env bash
# 真实安装与数据库生命周期集成测试；不使用真实账户或密钥。
set -Eeuo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export FERTILE_BOX_HOME=${FERTILE_BOX_HOME:-/tmp/fertile-bootstrap-test}
export FERTILE_POSTGRES_PORT=${FERTILE_POSTGRES_PORT:-55432}
logs=${FERTILE_TEST_LOGS:-$FERTILE_BOX_HOME/test-logs}
mkdir -p "$logs"
start=$SECONDS
bash "$repo/scripts/bootstrap-box.sh" | tee "$logs/first.log"
printf 'cold_seconds=%s\n' "$((SECONDS-start))" | tee "$logs/timing.env"
start=$SECONDS
bash "$repo/scripts/bootstrap-box.sh" | tee "$logs/second.log"
printf 'warm_seconds=%s\n' "$((SECONDS-start))" | tee -a "$logs/timing.env"
if grep -q '^INSTALL ' "$logs/second.log"; then echo "第二次仍安装工具" >&2; exit 1; fi
for item in system-deps rust uv python node pnpm llvm postgres; do
  grep -q "^SKIP $item" "$logs/second.log"
done
bash "$repo/scripts/check-toolchain.sh"
bash "$repo/scripts/postgres.sh" init
bash "$repo/scripts/postgres.sh" init
trap 'bash "$repo/scripts/postgres.sh" stop' EXIT
bash "$repo/scripts/postgres.sh" start
bash "$repo/scripts/postgres.sh" start
bash "$repo/scripts/postgres.sh" status
source "$FERTILE_BOX_HOME/fertile/env.sh"
socket="$FERTILE_ROOT/run/postgres"
psql -h "$socket" -p "$FERTILE_POSTGRES_PORT" -d postgres -v ON_ERROR_STOP=1 -c 'CREATE TABLE bootstrap_probe (value integer); INSERT INTO bootstrap_probe VALUES (42);'
[[ "$(psql -h "$socket" -p "$FERTILE_POSTGRES_PORT" -d postgres -Atc 'SHOW data_directory')" = "$FERTILE_ROOT/data/postgres" ]]
bash "$repo/scripts/postgres.sh" stop
bash "$repo/scripts/postgres.sh" start
[[ "$(psql -h "$socket" -p "$FERTILE_POSTGRES_PORT" -d postgres -Atc 'SELECT value FROM bootstrap_probe')" = 42 ]]
psql -h "$socket" -p "$FERTILE_POSTGRES_PORT" -d postgres -v ON_ERROR_STOP=1 -c 'DROP TABLE bootstrap_probe'
bash "$repo/scripts/postgres.sh" stop
if bash "$repo/scripts/postgres.sh" status; then echo "stop 后仍在运行" >&2; exit 1; fi
trap - EXIT
echo "PASS clean bootstrap, all skips, pinned tools, persistent PostgreSQL lifecycle"
