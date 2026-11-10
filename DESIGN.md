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

## Verification evidence

The verifier writes separate outputs for each run:

- an authoritative expected SHA-256 manifest derived from the captured inventory;
- a computed SHA-256 manifest from HashSuite;
- a computed SHA-512 manifest from HashSuite;
- a combined HashSuite stdout/stderr log.

The expected manifest is built from the complete inventory, while the computed manifests cover the selected paths in that run. The verifier also checks regular-file status, symlink components, exact size, and before/after file signatures. A zero process status is required for a successful verification run.

## Cache pruning

Pruning is deliberately conservative: if any selected path fails either checksum or size/stability validation, no selected cache objects are removed. Only corresponding objects in the Git LFS object cache are eligible. Incomplete-download storage is outside the pruning target.

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
