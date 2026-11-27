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
empty_tree=$(git -C "$target/web" mktree </dev/null)
wrong_tree_commit=$(git -C "$target/web" -c user.name='Contract test' -c user.email='contract-test@example.invalid' \
    commit-tree "$empty_tree" -m 'Fixture with different producer source')
printf '%s\n' "$wrong_tree_commit" > "$target/frontend/openapi/web.commit"
expect_failure 'different producer tree' 'has a different tree from gitlink'
git -C "$target/frontend" restore openapi/web.commit
printf '\n// deliberate Rust client drift\n' >> "$target/web/crates/finance-client/src/lib.rs"
expect_failure 'generated Rust client drift' 'deliberate Rust client drift'
git -C "$target/web" restore crates/finance-client/src/lib.rs
printf '\n// deliberate TypeScript client drift\n' >> "$target/frontend/src/api/generated/index.ts"
expect_failure 'generated TypeScript client drift' 'deliberate TypeScript client drift'
git -C "$target/frontend" restore src/api/generated/index.ts

if [[ $(git -C "$target/web" rev-parse HEAD) == 6e5fd80a56c3ab2fd0634fd69ecec3b7315dcd06 ]]; then
    # Reproduce the published U5 pin, whose generated client still used W2a.
    frontend_pin=$(git -C "$target/frontend" rev-parse HEAD)
    git -C "$target/frontend" checkout --quiet --detach adce47d05acbf58aeb95c4bd8562ef7ebdf36f16
    git -C "$target" add frontend
    LC_ALL=C expect_failure 'W6 paired with the stale U5 client' 'frontend/openapi/web.openapi.json differ'
    git -C "$target/frontend" checkout --quiet --detach "$frontend_pin"
    git -C "$target" add frontend
fi

if [[ $(git -C "$target/frontend" rev-parse HEAD) == 3788ae986ea61762b4219fd17547b5e2840c93cb ]]; then
    # The old W4 frontend baseline is allowed only with the reviewed W2a gitlink.
    review_pin=$(git -C "$target/web" rev-parse HEAD)
    next_web=$(git -C "$target/web" -c user.name='Contract test' -c user.email='contract-test@example.invalid' \
        commit-tree 'HEAD^{tree}' -p HEAD -m 'Fixture outside the reviewed W2a pin')
    git -C "$target/web" checkout --quiet --detach "$next_web"
    git -C "$target" add web
    expect_failure 'stale frontend baseline used outside the W2a pin' 'has a different tree from gitlink'
    git -C "$target/web" checkout --quiet --detach "$review_pin"
    git -C "$target" add web
fi

git -C "$target/finance" rev-parse HEAD > "$target/web/openapi/finance.commit"
stream=protocol/v1/market-status-sse.schema.json
sed -i '1s/^/ /' "$target/finance/$stream"
git -C "$target/finance" add -- "$stream"
git -C "$target/finance" -c user.name='Contract test' -c user.email='contract-test@example.invalid' \
    commit --quiet -m 'Fixture with changed versioned stream schema'
git -C "$target" add finance
expect_failure 'finance ancestor differs in stream schema' 'differ in versioned protocol files'
