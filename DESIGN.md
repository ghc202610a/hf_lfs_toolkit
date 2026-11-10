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
