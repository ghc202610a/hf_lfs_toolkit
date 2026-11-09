#!/bin/sh
# Fetch and materialize selected paths one at a time from the current LFS clone.
set -eu
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Run this from inside the Git working tree." >&2
    exit 2
fi
if ! git lfs version >/dev/null 2>&1; then
    echo "git-lfs is not installed or not available in PATH." >&2
    exit 2
fi
if [ "$#" -eq 0 ]; then
    echo "Usage: $0 path [path ...]" >&2
    exit 2
fi
for path do
    printf '\n==> Fetching/materializing: %s\n' "$path"
    git lfs pull --include="$path" --exclude=""
done
