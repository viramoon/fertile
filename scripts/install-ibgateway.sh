#!/usr/bin/env bash
# Ubuntu 24.04 x86-64. The two immutable release assets may also be staged offline.
set -euo pipefail
umask 077
repo=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=/dev/null
source "$repo/deploy/toolchain.env"
# shellcheck source=/dev/null
source "$repo/scripts/runtime-root.sh"
root=$(fertile_root)
cache=$root/cache/downloads/ibgateway
tools=$root/tools
gateway=ibgateway-$IB_GATEWAY_VERSION-standalone-linux-x64.sh
ibc=IBCLinux-$IBC_VERSION.zip
[[ $root == /* && $root != / && $(uname -sm) == 'Linux x86_64' ]] || exit 2
case ${1:-} in
  --verify-only|--offline|'') ;;
  *) echo "Usage: $0 [--verify-only|--offline]" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || exit 2
mkdir -p "$cache" "$tools"
exec 9>"$cache/install.lock"
flock 9
stage=$(mktemp -d "$tools/.ib-install.XXXXXXXX")
trap 'rm -rf -- "$stage"; [[ -z ${partial:-} ]] || rm -f -- "$partial"' EXIT

asset() {
    local name=$1 digest=$2 url=$3 mode=$4
    if [[ ! -f $cache/$name ]]; then
        [[ -z $mode ]] || { echo "Missing cached asset $name" >&2; return 1; }
        partial=$(mktemp "$cache/.download.XXXXXXXX")
        curl --fail --location --silent --show-error --retry 3 --connect-timeout 20 --max-time 1200 "$url" -o "$partial"
        printf '%s  %s\n' "$digest" "$partial" | sha256sum --check --status
        mv -T -- "$partial" "$cache/$name"
        partial=
    fi
    (cd "$cache" && printf '%s  %s\n' "$digest" "$name" | sha256sum --check)
}
asset "$gateway" "$IB_GATEWAY_SHA256" "https://github.com/gnzsnz/ib-gateway-docker/releases/download/ibgateway-stable%40$IB_GATEWAY_VERSION/$gateway" "${1:-}"
asset "$ibc" "$IBC_SHA256" "https://github.com/IbcAlpha/IBC/releases/download/$IBC_VERSION/$ibc" "${1:-}"
[[ ${1:-} != --verify-only ]] || exit 0

gw_target=$tools/ibgateway/$IB_GATEWAY_VERSION
ibc_target=$tools/ibc/$IBC_VERSION
mkdir -p "$tools/ibgateway" "$tools/ibc"
if [[ ! -e $gw_target ]]; then
    bash "$cache/$gateway" -q -dir "$stage/gateway"
    [[ -x $stage/gateway/ibgateway && -x $stage/gateway/jre/bin/java && -s $stage/gateway/.install4j/i4jruntime.jar ]]
    printf '%s\n' "$gw_target/jre" > "$stage/gateway/.install4j/inst_jre.cfg"
    printf '%s\n' "$IB_GATEWAY_SHA256" > "$stage/gateway/.fertile-asset-sha256"
    mv -T -- "$stage/gateway" "$gw_target"
fi
[[ $(cat "$gw_target/.fertile-asset-sha256") == "$IB_GATEWAY_SHA256" && -x $gw_target/jre/bin/java && -s $gw_target/.install4j/i4jruntime.jar ]]
[[ -x $gw_target/ibgateway || -x $gw_target/ibgateway1 ]]
if [[ ! -e $ibc_target ]]; then
    unzip -q "$cache/$ibc" -d "$stage/ibc"
    chmod u+x "$stage"/ibc/*.sh "$stage"/ibc/scripts/*.sh
    [[ -s $stage/ibc/IBC.jar && -x $stage/ibc/scripts/ibcstart.sh ]]
    printf '%s\n' "$IBC_SHA256" > "$stage/ibc/.fertile-asset-sha256"
    mv -T -- "$stage/ibc" "$ibc_target"
fi
[[ $(cat "$ibc_target/.fertile-asset-sha256") == "$IBC_SHA256" && -s $ibc_target/IBC.jar && -x $ibc_target/scripts/ibcstart.sh ]]
cc -Wall -Wextra -Werror -shared -fPIC -O2 "$repo/deploy/ibgateway/loopback.c" -ldl -o "$stage/loopback.so"
if ! cmp -s "$stage/loopback.so" "$tools/ibgateway/loopback.so"; then
    mv -T -- "$stage/loopback.so" "$tools/ibgateway/loopback.so"
fi
printf 'Ready: Gateway %s + IBC %s; paper loopback policy installed\n' "$IB_GATEWAY_VERSION" "$IBC_VERSION"
