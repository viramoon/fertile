#!/usr/bin/env bash
set -Eeuo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
mkdir "$tmp/bin"

cat > "$tmp/bin/apt-cache" <<'EOF'
#!/usr/bin/env bash
[[ $1 = policy && $LC_ALL = C ]] || exit 2
case "$FERTILE_TEST_APT_CASE:$2" in
  ubuntu:*|debian:libgtk-3-0|debian:libasound2)
    printf '%s:\n  Installed: (none)\n  Candidate: 1.0\n' "$2" ;;
  debian:libasound2t64)
    printf '%s:\n  Installed: (none)\n  Candidate: (none)\n' "$2" ;;
esac
EOF
cat > "$tmp/bin/dpkg-query" <<'EOF'
#!/usr/bin/env bash
if [[ $FERTILE_TEST_APT_CASE = installed && $3 = *t64 ]]; then
  printf 'install ok installed'
else
  exit 1
fi
EOF
cat > "$tmp/bin/apt-get" <<'EOF'
#!/usr/bin/env bash
touch "$FERTILE_TEST_APT_CALLED"
exit 3
EOF
cp "$tmp/bin/apt-get" "$tmp/bin/sudo"
chmod +x "$tmp/bin/"*

export PATH="$tmp/bin:$PATH"
export FERTILE_ROOT="$tmp/unused-root" FERTILE_TEST_APT_CALLED="$tmp/apt-called"
packages() {
  FERTILE_TEST_APT_CASE=$1 "$repo/scripts/bootstrap-box.sh" --system-packages
}

for test_case in ubuntu installed; do
  selected=$(packages "$test_case")
  grep -Fxq libgtk-3-0t64 <<< "$selected"
  grep -Fxq libasound2t64 <<< "$selected"
  if grep -Exq 'libgtk-3-0|libasound2' <<< "$selected"; then exit 1; fi
done

selected=$(packages debian)
grep -Fxq libgtk-3-0 <<< "$selected"
grep -Fxq libasound2 <<< "$selected"
grep -Fxq build-essential <<< "$selected"
grep -Fxq libgl1 <<< "$selected"
if grep -q 't64$' <<< "$selected"; then exit 1; fi

if packages unavailable > "$tmp/packages" 2> "$tmp/error"; then
  echo 'Expected package resolution to fail when neither GTK package exists' >&2
  exit 1
fi
grep -Fq 'libgtk-3-0t64 libgtk-3-0' "$tmp/error"
[[ ! -s "$tmp/packages" && ! -e $FERTILE_ROOT && ! -e $FERTILE_TEST_APT_CALLED ]]
echo 'PASS bootstrap package selection (t64, Debian fallback, installed package, unavailable package)'
