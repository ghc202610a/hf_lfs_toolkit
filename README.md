# Hugging Face Git LFS snapshot and verification toolkit

This toolkit captures the Git LFS object inventory before materialization, pulls selected files, verifies the materialized bytes against the captured LFS SHA-256 OIDs and sizes, and writes ordinary SHA-256 and SHA-512 checksum manifests.

## Requirements

- Python 3.9 or newer (standard library only)
- Git
- Git LFS (`git-lfs`)
- Network access to the Hugging Face repository

No third-party Python packages are required. `hashcore.py` is included and computes SHA-256 and SHA-512 together in one streaming pass (8 MiB blocks by default).

## 1. Capture the pointer-only snapshot

From a directory where you want the snapshot created:

```sh
./hf-lfs-snapshot.py https://huggingface.co/ORG/MODEL --archive
```

The command clones with `GIT_LFS_SKIP_SMUDGE=1`, so LFS pointer files remain in the working tree. It records:

- `lfs-inventory.json`: durable per-path LFS SHA-256 OID and byte size, plus repository and commit metadata
- `lfs-listing.raw.txt`: original `git lfs ls-files --long --size` output
- `clone/`: pointer-only Git working tree

The inventory is captured before any pull. Preserve it independently if you later discard the clone or `.git` directory.

## 2. Pull selected files

Change into the snapshot's clone directory and pull paths one at a time:

```sh
cd hf_MODEL_TIMESTAMP/clone
../lfspull.sh path/to/file1 path/to/file2
```

`lfspull.sh` uses `git lfs pull --include=... --exclude=''` for each path. Paths containing shell whitespace should be quoted. Avoid broad include patterns unless you intend to materialize all matching files.

## 3. Verify and create independent manifests

Run from anywhere inside the clone working tree:

```sh
../verify-lfs.py --inventory ../lfs-inventory.json
```

By default it verifies every inventory entry. It checks file existence, exact byte size, and SHA-256 against the captured LFS OID. For each successful file it also records SHA-256 and SHA-512 in the conventional two-space checksum format:

- `SHA256SUMS`
- `SHA512SUMS`

Both digests are calculated during the same streaming read of each file. The ordinary manifests are independent of Git LFS and can subsequently be checked with standard tools such as `sha256sum -c SHA256SUMS` and `sha512sum -c SHA512SUMS` from the same working directory.

To verify only selected inventory paths:

```sh
../verify-lfs.py --inventory ../lfs-inventory.json path/to/file1 path/to/file2
```

To choose manifest destinations:

```sh
../verify-lfs.py --inventory ../lfs-inventory.json \
  --sha256-out model.sha256 --sha512-out model.sha512
```

A failed file is excluded from the generated manifests and causes a nonzero exit status. A successful subset may still produce manifests, so inspect the final result line and exit status.

## Optional LFS cache pruning

`--prune` removes the corresponding local `.git/lfs/objects/...` cache object only after the materialized working-tree file passes both size and SHA-256 checks:

```sh
../verify-lfs.py --inventory ../lfs-inventory.json --prune
```

This is optional. It does not remove the materialized working-tree file or the captured inventory. Do not use it if you want to retain the local LFS object cache.

## Notes and limitations

- The captured LFS SHA-256 OID is the authority for initial verification. The pointer stub itself is not needed after the inventory has been captured.
- The inventory describes the selected Git commit at snapshot time. If the working tree is changed to another commit, the inventory may no longer describe its LFS files.
- The verifier expects to run inside the matching Git working tree so it can resolve the repository root. It can still write independent manifests before the `.git` directory is discarded.
- The snapshot parser expects the four-field `git lfs ls-files --long --size` format: full 64-hex OID, status marker, byte size, and path. The path is retained as the remainder of the line, including embedded spaces.
- No live Hugging Face network clone was performed as part of local packaging tests. Test the first snapshot against the intended repository and retain the inventory before pulling.
