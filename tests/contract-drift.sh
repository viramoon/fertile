#!/usr/bin/env bash
# Exercise real drift detection in disposable checkouts; never alter a developer's snapshots or clients.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
target=$work/fertile
git clone --quiet --local --no-hardlinks --no-checkout "$root" "$target"
git -C "$target" checkout --quiet --detach "$(git -C "$root" rev-parse HEAD)"
git -C "$target" -c protocol.file.allow=always \
    -c "submodule.finance.url=$root/finance" -c "submodule.web.url=$root/web" \
    -c "submodule.frontend.url=$root/frontend" submodule update --quiet --init
[[ -d $root/frontend/node_modules ]] || { echo 'Install frontend dependencies with pnpm first' >&2; exit 1; }
ln -s "$root/frontend/node_modules" "$target/frontend/node_modules"

expect_failure() {
    local case_name=$1 match=$2
    if (cd "$target" && bash scripts/contract-diff.sh) > "$work/output" 2>&1; then
        echo "Unexpected pass: $case_name" >&2; return 1
    fi
    if ! grep -Fq -- "$match" "$work/output"; then
        cat "$work/output" >&2
        echo "Unexpected error: $case_name" >&2; return 1
    fi
    printf 'Detected %s\n' "$case_name"
}

# Keep JSON valid: semantically identical reformatting still violates the byte-for-byte export.
sed -i '1s/^/ /' "$target/contracts/finance.openapi.json"
expect_failure 'producer snapshot drift' 'finance.openapi.json'
git -C "$target" restore contracts/finance.openapi.json
printf '%040d\n' 0 > "$target/frontend/openapi/web.commit"
expect_failure 'wrong producer revision' 'web.commit'
git -C "$target/frontend" restore openapi/web.commit
printf '\n// deliberate Rust client drift\n' >> "$target/web/crates/finance-client/src/lib.rs"
expect_failure 'generated Rust client drift' 'deliberate Rust client drift'
git -C "$target/web" restore crates/finance-client/src/lib.rs
printf '\n// deliberate TypeScript client drift\n' >> "$target/frontend/src/api/generated/index.ts"
expect_failure 'generated TypeScript client drift' 'deliberate TypeScript client drift'
