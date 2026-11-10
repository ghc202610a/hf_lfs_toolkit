# Design and data model

## Workflow boundaries

The toolkit is organized around separate stages:

1. **Snapshot** — clone a repository with Git LFS smudging disabled and capture the Git commit plus LFS metadata.
2. **Acquisition** — materialize selected LFS paths using Git LFS.
3. **Verification** — compare materialized bytes with the captured SHA-256 OID and expected size using HashSuite.
4. **Cache management** — optionally remove local LFS cache objects only after successful verification.

A successful stage must not be treated as proof that a later stage succeeded. In particular, a completed pull is not equivalent to a verified file.

## Raw and normalized inventory

`local.lfsfiles.json` preserves the raw JSON emitted by `git lfs ls-files --json`. `lfs-inventory.json` is the normalized toolkit representation and currently uses the format identifier `hf-lfs-inventory-v1`.

Top-level inventory fields:

- `format` — inventory format identifier
- `repository` — source repository URL
- `captured_at_utc` — UTC capture timestamp
- `git_commit` — checked-out commit captured by Git
- `source` — source command used to obtain raw LFS metadata
- `entries` — ordered array of normalized LFS entries

Each normalized entry contains:

- `path` — repository-relative path
- `oid` — 64-character lowercase SHA-256 LFS object ID
- `size` — expected byte size, integer and nonnegative
- `oid_type` — currently `sha256`
- `version` — Git LFS pointer version URL
- `checkout` and `downloaded` — copied from the raw entry when present

The raw capture is retained so that normalization does not destroy source details. Do not silently redefine the meaning of an existing field. A future incompatible change should use a new format identifier and an explicit migration or compatibility plan.

## Acquisition reliability

`lfspull.py` consumes the immutable captured inventory and invokes Git LFS once per selected path. Transfers are serial to keep resource use and failure attribution predictable. Each path is checked before acquisition; an already regular file with the expected size is skipped unless explicitly forced. Missing or undersized paths are eligible for pull, while symlinks, non-regular paths, and oversized files stop the run rather than being passed to Git LFS.

A disk-space preflight estimates the bytes required by selected paths not already at expected size, plus a configurable reserve. It checks the worktree filesystem only; it does not inspect a separately configured Git LFS media directory. It is not a reservation or guarantee: other processes can consume space after the check. Retry handling is bounded and uses exponential backoff. Every run appends JSON Lines events and flushes each record so an interrupted run leaves diagnostic evidence.

The runner delegates transfer and incomplete-object handling to Git LFS. It never removes or edits `.git/lfs/incomplete`, never prunes cache objects, and does not equate successful transfer or expected-size equality with checksum verification. Re-running the command is the recovery path; Git LFS remains responsible for its own cache and transfer-resume behavior.

## Verification evidence

The verifier writes separate outputs for each run:

- an authoritative expected SHA-256 manifest derived from the captured inventory;
- a computed SHA-256 manifest from HashSuite;
- a computed SHA-512 manifest from HashSuite;
- a combined HashSuite stdout/stderr log.

The expected manifest is built from the complete inventory, while the computed manifests cover the selected paths in that run. The verifier also checks regular-file status, symlink components, exact size, and before/after file signatures. A zero process status is required for a successful verification run.

## Cache pruning

Pruning is deliberately conservative: if any selected path fails either checksum or size/stability validation, no selected cache objects are removed. Only corresponding objects in the Git LFS object cache are eligible. Incomplete-download storage is outside the pruning target.

## Read-only cache inventory

`lfscache.py` discovers the configured Git LFS `LocalMediaDir` through `git lfs env` (or accepts an explicit cache directory) and scans only the canonical SHA-256 object layout under that directory's `objects` subtree. An object is recognized only when its relative path is exactly `aa/bb/<64-character-oid>`. Symlinks are not followed. Non-canonical files are ignored.

The report correlates cached OIDs with one captured inventory and reports matching objects, missing inventory objects, cached-size mismatches, and objects not referenced by that inventory. The latter category must not be called orphaned: Git LFS media storage may be shared across repositories, worktrees, and snapshots, and a single inventory cannot establish global reachability.

N+4 deliberately adds inspection only. The reporter is read-only, does not enter `.git/lfs/incomplete`, and does not delete, move, rewrite, or repair cache objects. Any future cleanup feature needs a separate design for global reachability, concurrent Git LFS processes, dry-run review, and explicit destructive authorization.

## Mutable local state

`lfs-inventory.json` remains the immutable expected snapshot. `local.lfsstate.json` is a separate mutable observation document with format identifier `hf-lfs-state-v1`. It is stored beside the inventory by default and includes:

- `inventory_sha256` — SHA-256 fingerprint of the exact inventory file bytes;
- `updated_at_utc` — last state update time;
- `entries` — path-keyed current observations, each with status, observation time, and a filesystem signature when a regular file is present;
- `verification_history` — append-only-in-practice per-path verification events, including outcome, timestamp, inventory fingerprint, file signature, exit code, and bounded diagnostic output.

If the inventory fingerprint changes, prior cached state is discarded rather than being applied to a different snapshot. A verification event is considered current only when its inventory fingerprint and file signature match the current inventory and filesystem observation. The reporter never treats expected-size equality as cryptographic verification.

The current status vocabulary is:

- `POINTER` — pointer content agrees with the captured OID and size;
- `MISSING` — path does not exist;
- `PARTIAL` — regular file is shorter than the expected size;
- `SIZE_OK` — regular file size matches, but no current successful verification evidence applies;
- `VERIFIED` — a successful HashSuite verification applies to the current signature;
- `MISMATCH` — pointer metadata, file size, or content verification disagrees with the inventory;
- `STALE` — verification evidence exists but belongs to a different file signature or inventory;
- `UNKNOWN` — symlink, non-regular file, inaccessible path, or unresolved condition.

Live filesystem evidence takes precedence over cached status. Symlink components are rejected before inspection so a repository-relative path cannot redirect observation outside the worktree. `--audit` is read-only; ordinary reporting refreshes the selected observations. `--verify` delegates hashing to the existing verifier one file at a time and does not prune cache objects.
