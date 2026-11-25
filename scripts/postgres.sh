#!/usr/bin/env bash
set -Eeuo pipefail
root=${FERTILE_ROOT:-${FERTILE_BOX_HOME:-/home/box}/fertile}
if [[ -f "$root/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$root/env.sh"
fi
export FERTILE_ROOT="$root"
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
  foreground)
    [[ -f "$data/PG_VERSION" ]] || { echo "先运行 postgres.sh init" >&2; exit 1; }
    mkdir -p "$socket"; chmod 700 "$socket"
    extra=()
    if [[ ${FERTILE_WEB_PEER:-0} == 1 ]]; then
      database=${FERTILE_WEB_DATABASE:-web}
      [[ "$database" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ && "$database" != postgres && "$database" != template0 && "$database" != template1 ]] || exit 2
      # Keep the cluster's real authentication files untouched. PG17 supports include in both.
      umask 077
      for setting in hba ident; do
        original=$(postgres -D "$data" -C "${setting}_file")
        [[ "$original" == /* ]] || original=$data/$original
        [[ -f "$original" && "$original" != *$'\n'* && "$original" != *'"'* && "$original" != *\\* ]] || exit 2
        generated=$(mktemp "$socket/.${setting}-XXXXXXXX")
        if [[ $setting == hba ]]; then
          printf 'local "%s" fertile_web peer map=fertile_managed_web\nlocal all fertile_web reject\n' "$database" > "$generated"
        else
          user=$(id -un)
          [[ $user =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$ ]] || exit 2
          printf 'fertile_managed_web "%s" fertile_web\n' "$user" > "$generated"
        fi
        printf 'include "%s"\n' "$original" >> "$generated"
        mv -T -- "$generated" "$socket/${setting}.conf"
        extra+=(-c "${setting}_file=$socket/${setting}.conf")
      done
    fi
    exec postgres -D "$data" -h "" -k "$socket" -p "$port" "${extra[@]}"
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
