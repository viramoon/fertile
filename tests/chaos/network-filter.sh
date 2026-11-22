#!/usr/bin/env bash
# Private helper. Its stdin is a liveness pipe kept open only by network.py.
set -euo pipefail
[[ $# == 3 && $1 =~ ^(direct|sudo)$ && $2 =~ ^[0-9]{3}$ && $3 =~ ^fertile-drill-[0-9a-f]{32}$ ]] || exit 2
duration=$2
(( duration >= 120 && duration <= 300 )) || exit 2
label=$3
filter=(iptables -w 4)
if [[ $1 == sudo ]]; then filter=(sudo -n -- "${filter[@]}"); fi
rule=(-o lo -p tcp -d 127.0.0.1 --dport 4002 -m comment --comment "$label" -j DROP)
remove() {
  local rc=0
  "${filter[@]}" -C OUTPUT "${rule[@]}" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) "${filter[@]}" -D OUTPUT "${rule[@]}";;
    1) "${filter[@]}" -S OUTPUT >/dev/null;; # Distinguish absence from lack of permission.
    *) return "$rc";;
  esac
}
# A separate process keeps an upper bound even if this helper itself is killed.
# A FIFO read lets us disarm it without orphaning a sleeping child.
scratch=$(mktemp -d)
mkfifo -m 600 "$scratch/disarm"
exec 3<>"$scratch/disarm"
rm -f "$scratch/disarm"
rmdir "$scratch"
(read -r -t "$((duration + 3))" -u 3 _ && exit 0; remove) </dev/null >/dev/null &
watchdog=$!
cleanup() {
  if remove; then
    printf 'done\n' >&3
    wait "$watchdog" || true
  else
    echo "Restore failed for $label; the detached fallback is still armed" >&2
    return 1
  fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
"${filter[@]}" -I OUTPUT 1 "${rule[@]}"
read -r started _ </proc/uptime
printf 'active %s\n' "$started"
remaining=$duration
while (( remaining > 0 )); do
  interval=$((remaining < 30 ? remaining : 30))
  rc=0
  read -r -t "$interval" _ || rc=$?
  (( rc > 128 )) || break # EOF when parent is interrupted
  remaining=$((remaining - interval))
  "${filter[@]}" -C OUTPUT "${rule[@]}" # Also retain the noninteractive sudo session.
done
remove
read -r ended _ </proc/uptime
printf 'cleared %s\n' "$ended"
