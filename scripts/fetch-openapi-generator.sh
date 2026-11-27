#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '::error::OpenAPI Generator JAR: %s\n' "$1" >&2
  exit 1
}

if [[ $# != 4 ]]; then
  fail 'Expected cache-hit, HTTPS URL, SHA-256 and destination.'
fi
cache_hit=$1
url=$2
expected=$3
jar=$4
[[ $url == https://* && $expected =~ ^[0-9a-f]{64}$ ]] ||
  fail 'Expected a pinned HTTPS URL and lowercase SHA-256.'

verify() {
  local file=$1 actual
  [[ -f $file && ! -L $file ]] || fail "$2 file is missing or is not a regular file."
  actual=$(sha256sum < "$file") || fail "Cannot read $2 file."
  [[ $actual == "$expected  -" ]] || fail "$2 SHA-256 mismatch."
}

case "$cache_hit" in
  true)
    verify "$jar" 'Restored'
    exit 0
    ;;
  false) ;;
  *) fail 'Expected cache-hit to be true or false.' ;;
esac

partial=$(mktemp -- "$jar.part.XXXXXX") || fail 'Cannot create temporary download file.'
trap 'rm -f -- "$partial"' EXIT
for delay in 1 2 4 0; do
  if http=$(curl --disable --fail --location --silent --proto '=https' --proto-redir '=https' \
      --connect-timeout 10 --max-time 60 --output "$partial" --write-out '%{http_code}' -- "$url"); then
    break
  else
    status=$?
  fi
  reason="curl exit $status"
  if [[ $http =~ ^[1-5][0-9]{2}$ ]]; then
    reason="HTTP $http ($reason)"
  fi
  case "$status:$http" in
    22:408|22:429|22:5[0-9][0-9]|6:*|7:*|18:*|28:*|52:*|55:*|56:*) ;;
    *) fail "Download failed ($reason)." ;;
  esac
  (( delay > 0 )) || fail "Download failed after 4 attempts ($reason)."
  printf '::warning::OpenAPI Generator JAR download failed (%s). Retrying in %s seconds.\n' \
    "$reason" "$delay" >&2
  sleep "$delay"
done

verify "$partial" 'Downloaded'
mv -T -- "$partial" "$jar" || fail 'Cannot install verified download.'
