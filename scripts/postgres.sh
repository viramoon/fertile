#!/usr/bin/env bash
set -Eeuo pipefail
source "${FERTILE_BOX_HOME:-/home/box}/fertile/env.sh"
data="$FERTILE_ROOT/data/postgres"
socket="$FERTILE_ROOT/run/postgres"
port=${FERTILE_POSTGRES_PORT:-5432}
[[ "$port" =~ ^[0-9]+$ && "$port" -gt 1023 && "$port" -lt 65536 ]] ||
  { echo "无效的非特权端口：$port" >&2; exit 2; }
(( EUID != 0 )) || { echo "Postgres 必须由非 root 用户运行。" >&2; exit 2; }
case "${1:-}" in
  init)
    if [[ -f "$data/PG_VERSION" ]]; then
      [[ "$(cat "$data/PG_VERSION")" = "$(pg_ctl --version | sed -E 's/.* ([0-9]+).*/\1/')" ]] ||
        { echo "已有数据主版本不兼容，禁止自动升级或覆盖。" >&2; exit 1; }
      echo "SKIP postgres init"; exit
    fi
    mkdir -p "$socket"
    chmod 700 "$socket"
    initdb -D "$data" --encoding=UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256
    ;;
  start)
    [[ -f "$data/PG_VERSION" ]] || { echo "先运行 scripts/postgres.sh init" >&2; exit 1; }
    if pg_ctl -D "$data" status >/dev/null 2>&1; then echo "SKIP postgres start"; exit; fi
    mkdir -p "$socket" "$FERTILE_ROOT/logs"
    chmod 700 "$socket"
    pg_ctl -D "$data" -l "$FERTILE_ROOT/logs/postgres.log" \
      -o "-h 127.0.0.1 -k $socket -p $port" -w start
    ;;
  stop)
    if pg_ctl -D "$data" status >/dev/null 2>&1; then
      pg_ctl -D "$data" -m fast -w stop
    else echo "SKIP postgres stop"; fi
    ;;
  status) pg_ctl -D "$data" status ;;
  *) echo "用法：scripts/postgres.sh init|start|stop|status" >&2; exit 2 ;;
esac
