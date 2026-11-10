# Hugging Face Git LFS snapshot and verification toolkit

This repository contains small command-line tools for capturing the Git LFS inventory of a Hugging Face repository, materializing selected LFS files, and checking the resulting working-tree files against the captured LFS SHA-256 object IDs and byte sizes.

The snapshot records the Git commit and preserves both the raw `git lfs ls-files --json` response and a normalized inventory. Verification uses the separate [HashSuite](https://github.com/ghc202610a/hashsuite) project as its hashing engine; this repository does not contain a second hash implementation.

## Requirements

- Python 3.9 or newer
- Git
- Git LFS (`git-lfs`) with support for `git lfs ls-files --json`
- Network access and any required credentials for the Hugging Face repository
- HashSuite `hashcheck.py` for verification (not required just to capture a snapshot)

The Python tools use only the Python standard library. HashSuite is a separate dependency and must be obtained and maintained separately.

## 1. Capture a pointer-only snapshot

Run `hf-lfs-snapshot.py` from this repository or use its full path:

```sh
./hf-lfs-snapshot.py https://huggingface.co/ORG/MODEL
```

Optional destination controls:

```sh
./hf-lfs-snapshot.py https://huggingface.co/ORG/MODEL \
  --out /data/model-snapshots --name model-capture-01
```

The command clones with `GIT_LFS_SKIP_SMUDGE=1`, leaving LFS pointer files in the working tree while it records the inventory. The output is a new directory (default name `hf_MODEL_YYYYMMDD_HHMMSS`) containing:

| Path | Purpose |
|---|---|
| `clone/` | Pointer-only Git working tree at the captured checkout |
| `local.lfsfiles.json` | Raw output from `git lfs ls-files --json` |
| `local.lfsfiles.txt` | Human-readable `git lfs ls-files --long --size` listing |
| `lfs-inventory.json` | Normalized `hf-lfs-inventory-v1` inventory, including repository URL, capture time, Git commit, LFS paths, SHA-256 OIDs and sizes |
| `_clone.sh` | Re-clones the repository and checks out the captured commit; refuses to overwrite an existing destination |
| `local.lfspull.sh` | Pulls the captured LFS paths one at a time from the sibling `clone/` directory |

A compressed `.tar.gz` archive of the snapshot directory is created by default beside the directory. Use `--no-archive` to suppress that archive. The option is an opt-out; there is no `--archive` flag.

The inventory is captured before any LFS pull. Preserve `lfs-inventory.json` and the raw listing independently if you later remove the clone or its `.git` directory.

## 2. Materialize LFS files

To pull every path in the captured inventory, run the generated helper from the snapshot directory:

```sh
cd /path/to/snapshot
./local.lfspull.sh
```

The helper changes into `clone/` and invokes `git lfs pull --include=...` once per captured path. This can take time and disk space for large repositories.

To pull only selected paths, use this repository's `lfspull.sh` while the current directory is the clone working tree:

```sh
cd /path/to/snapshot/clone
/path/to/hf_lfs_toolkit/lfspull.sh \
  'path/to/file one.safetensors' path/to/another-file.bin
```

Each path is passed as a separately quoted include value. Avoid broad patterns unless you intend to materialize all matching files. Git LFS authentication and remote access are handled by Git/Git LFS, not by this toolkit.

For retry handling, disk-space preflight, and a durable per-run log, use the inventory-driven Python runner from the clone working tree:

```sh
python3 /path/to/hf_lfs_toolkit/lfspull.py \
  --inventory ../lfs-inventory.json \
  --retries 3 --retry-delay 5
```

The runner processes selected paths serially and invokes `git lfs pull` separately for each path. It retries failed pulls with exponential backoff, checks available space before starting, and writes JSON Lines events to `local.lfspull.TIMESTAMP.jsonl` (or the path supplied with `--log`). A rerun is safe: Git LFS manages its own local object and incomplete-transfer data, which this tool does not delete or rewrite. Exact-size working-tree files are skipped by default; this is an acquisition shortcut, not cryptographic verification. Use `--force` to invoke Git LFS for those paths too.

The preflight estimates required space as the sum of expected sizes for paths that are not already exact-size files, plus a default 1 GiB reserve. It checks the worktree filesystem only; it does not inspect a separately configured Git LFS media directory, and it is not a reservation or guarantee. Adjust the reserve with `--reserve-gib`, or explicitly disable the check with `--no-space-check`. Select exact paths or repeatable globs with positional paths and `--include`. A nonzero exit status means at least one selected path did not complete with the expected size; inspect the run log and rerun the failed paths as needed. A `SIZE_OK` result does not establish SHA-256 integrity; use `verify-lfs.py` or `lfsls.py --verify` for that.

The generated `_clone.sh` can recreate the pointer-only checkout at the captured commit:

```sh
cd /path/to/snapshot
./_clone.sh [new-clone-directory]
```

It refuses to use a destination that already exists. The script does not embed credentials or preserve arbitrary local Git configuration.

## 3. Verify materialized files

Verification is performed from inside the matching Git working tree because the verifier resolves the worktree root using Git:

```sh
cd /path/to/snapshot/clone
/path/to/hf_lfs_toolkit/verify-lfs.py --inventory ../lfs-inventory.json
```

By default, all inventory entries are selected. Specific paths may be supplied as positional arguments:

```sh
/path/to/hf_lfs_toolkit/verify-lfs.py \
  --inventory ../lfs-inventory.json \
  'path/to/file one.safetensors' path/to/another-file.bin
```

The verifier checks that selected paths are regular files, rejects symlinks in any path component, checks exact byte sizes, and asks HashSuite to verify SHA-256 against the captured LFS OIDs. It also compares file signatures before and after hashing to detect files that changed while being read.

### HashSuite discovery

The verifier searches for HashSuite `hashcheck.py` in this order:

1. `--hashcheck /path/to/hashcheck.py`
2. `HASHSUITE_HASHCHECK` environment variable
3. `HASHSUITE_DIR` environment variable (the directory containing `hashcheck.py`)
4. `hashcheck.py` on `PATH`

For example:

```sh
export HASHSUITE_DIR="$HOME/src/hashsuite"
/path/to/hf_lfs_toolkit/verify-lfs.py --inventory ../lfs-inventory.json
```

Or pass the script directly:

```sh
/path/to/hf_lfs_toolkit/verify-lfs.py \
  --hashcheck "$HOME/src/hashsuite/hashcheck.py" \
  --inventory ../lfs-inventory.json
```

The expected HashSuite revision used when this toolkit revision was assembled is recorded in `UPSTREAM-REVISIONS.txt`. HashSuite is not vendored here.

### Per-run outputs

Each verifier run generates a run identifier by default (UTC timestamp plus Unix time). Output files are written in the current working directory:

- `local.sha256sums.check.RUNID.txt` — authoritative SHA-256 manifest derived from the complete captured inventory
- `local.sha256sums.RUNID.txt` — computed SHA-256 manifest from HashSuite
- `local.sha256sums.sha512.RUNID.txt` — computed SHA-512 manifest from HashSuite
- `local.hashcheck.RUNID.log` — combined HashSuite stdout and stderr

The authoritative manifest represents the full inventory even when only a subset of paths is selected for verification. HashSuite's computed manifests represent the selected paths processed in that run. These files are separate per run rather than cumulative fixed-name manifests. `--run-id` can be used to supply a chosen identifier; it must contain only letters, digits, underscore, dot, or hyphen.

Useful options:

- `--jobs N` — HashSuite worker count (default: up to 8, bounded by available CPUs)
- `--chunk-mib N` — HashSuite read chunk size in MiB (default: 8)
- `--no-advice` — pass the corresponding option to HashSuite
- `--manifest-out PATH` — choose the authoritative expected manifest path
- `--sha256-out PATH` — choose the computed SHA-256 output path
- `--sha512-out PATH` — choose the computed SHA-512 output path
- `--log-out PATH` — choose the combined HashSuite log path
- `--run-id ID` — choose the run identifier used in default output names

A failed verification returns a nonzero status. A subset of successful files may still appear in computed manifests; inspect the final `LFS result` line and the process exit status before treating the run as successful.

## 4. Inspect local LFS state

Run the state reporter from inside the matching Git working tree:

```sh
cd /path/to/snapshot/clone
python3 /path/to/hf_lfs_toolkit/lfsls.py --inventory ../lfs-inventory.json
```

The reporter writes `local.lfsstate.json` beside the inventory by default. This file is a mutable observation record; it does not modify `lfs-inventory.json`, which remains the immutable expected snapshot. The state file is tied to the exact inventory bytes by a SHA-256 fingerprint. A different inventory invalidates the old cached state.

Reported statuses are:

- `POINTER` — a valid Git LFS pointer matching the captured OID and size is present.
- `MISSING` — the inventory path is absent.
- `PARTIAL` — a regular file exists but is smaller than the expected size.
- `SIZE_OK` — a regular file has the expected size, but has no current matching verification record.
- `VERIFIED` — HashSuite verification succeeded for the current file signature and inventory.
- `MISMATCH` — a pointer/size mismatch or a failed checksum was observed.
- `STALE` — previous verification evidence exists, but the file signature or inventory has changed.
- `UNKNOWN` — the path is a symlink, non-regular, inaccessible, or otherwise unresolved.

Size equality alone is not cryptographic verification. Filesystem observations take precedence over cached state. Verification evidence is retained as a history and only applies to the exact file signature and inventory fingerprint recorded with it.

Useful reporting options:

```sh
python3 /path/to/hf_lfs_toolkit/lfsls.py --status VERIFIED
python3 /path/to/hf_lfs_toolkit/lfsls.py --status MISSING,PARTIAL
python3 /path/to/hf_lfs_toolkit/lfsls.py --include '*.safetensors' --json
python3 /path/to/hf_lfs_toolkit/lfsls.py --audit
```

`--include` accepts repeatable shell-style path patterns. `--audit` compares current observations with the saved state without writing it and returns status 1 when changes are found. `--json` emits machine-readable results.

To verify eligible materialized files through the existing HashSuite-backed verifier:

```sh
python3 /path/to/hf_lfs_toolkit/lfsls.py \
  --inventory ../lfs-inventory.json --verify --hashcheck "$HOME/src/hashsuite/hashcheck.py"
```

`--verify` invokes `verify-lfs.py` separately for each selected `SIZE_OK` or `STALE` file, records the result, and does not enable cache pruning. Missing, pointer, partial, and otherwise unresolved files are reported but not sent to HashSuite.

## 5. Optional LFS cache pruning

`--prune` removes corresponding local Git LFS cache objects only when every selected file passes the complete SHA-256 and size/stability checks:

```sh
/path/to/hf_lfs_toolkit/verify-lfs.py \
  --inventory ../lfs-inventory.json --prune
```

Pruning affects objects under the Git directory's `lfs/objects` hierarchy. It does not remove the materialized working-tree files or the captured inventory. If any selected file fails, pruning is skipped for the entire selected batch. Use this option only when you intentionally want to discard the verified local LFS cache objects.

## 6. Inspect the local Git LFS cache

Run from inside the Git working tree associated with the captured inventory:

```sh
python3 /path/to/hf_lfs_toolkit/lfscache.py --inventory ../lfs-inventory.json
```

The reporter discovers Git LFS's configured `LocalMediaDir` using `git lfs env` and scans only its `objects` directory. Use `--cache-dir PATH` to inspect a specific object directory. It reports total object count and bytes, objects whose SHA-256 OIDs occur in this inventory, objects not referenced by this inventory, missing inventory OIDs, cached-size mismatches, and skipped symlinks/unreadable entries.

For machine-readable output or per-object detail:

```sh
python3 /path/to/hf_lfs_toolkit/lfscache.py --inventory ../lfs-inventory.json --json
python3 /path/to/hf_lfs_toolkit/lfscache.py --inventory ../lfs-inventory.json --show-objects
```

This command is strictly read-only. “Not referenced by this inventory” does not mean orphaned: Git LFS media storage can be shared by multiple repositories and snapshots. Do not delete such objects based on one inventory. The scanner traverses only the configured `objects` directory; it does not enter `.git/lfs/incomplete`, and it does not perform cleanup.

## Inventory format and limitations

The normalized inventory format identifier is `hf-lfs-inventory-v1`. Each entry records a relative path, SHA-256 OID, byte size, OID type, and Git LFS pointer version. The snapshot parser validates JSON structure, duplicate paths, path traversal, OID format, size type, and pointer version.

The inventory describes the captured Git commit. If the working tree is checked out to another commit or its tracked files are changed, the inventory may no longer describe the files present. Verification checks the inventory paths against the current worktree; it does not establish that the remote repository still has the same branch tip.

The snapshot tool does not perform a live Hugging Face network operation as part of its local unit tests. Test a real snapshot against the intended repository and retain the inventory before materializing files.

## Tests

Run the toolkit's current regression tests from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

The regression suite includes snapshot/verifier safety tests, local-state classification tests, acquisition-runner tests, and read-only cache-inventory tests. These deterministic tests do not constitute live Hugging Face cloning, Git LFS transfer, or real HashSuite integration coverage. See [DEVELOPMENT.md](DEVELOPMENT.md) for the exact current test scope.
