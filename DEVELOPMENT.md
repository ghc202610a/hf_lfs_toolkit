# Development notes

## Repository layout

- `hf-lfs-snapshot.py` — clones a Hugging Face Git repository with LFS smudging disabled, captures raw and normalized LFS inventories, and emits helper scripts.
- `verify-lfs.py` — validates inventory entries and working-tree paths, invokes external HashSuite `hashcheck.py`, checks file size/stability, and optionally prunes verified LFS cache objects.
- `lfspull.sh` — static helper for pulling explicitly named paths from the current Git working tree.
- `tests/test_n1_regression.py` — current focused regression tests.
- `UPSTREAM-REVISIONS.txt` — pinned source revisions used as upstream references for the toolkit and HashSuite.

## Runtime dependencies

The snapshot tool requires Python 3.9+, Git, and Git LFS. The verifier additionally requires the separate HashSuite `hashcheck.py` script. HashSuite discovery is controlled by `--hashcheck`, `HASHSUITE_HASHCHECK`, `HASHSUITE_DIR`, or `PATH`; see README for the exact precedence.

No third-party Python package is required by the toolkit scripts themselves.

## Local validation

From the repository root:

```sh
python3 -m py_compile hf-lfs-snapshot.py verify-lfs.py
python3 -m unittest discover -s tests -v
```

The current unit tests are deliberately small. They exercise duplicate-path rejection in the snapshot JSON parser and rejection of a symlink in a verifier inventory path. They do not perform a live clone, LFS download, real HashSuite hashing, archive round-trip, or cache-pruning integration test.

When changing verifier behavior, use a temporary Git worktree and a controlled HashSuite fixture or the pinned HashSuite checkout. Test both successful and failing checksum cases, size mismatch, missing/non-regular paths, symlinks in intermediate components, file mutation during hashing, subprocess error handling, and the all-or-none prune rule.

When changing snapshot behavior, test malformed JSON, duplicate paths, invalid OIDs and sizes, unsafe paths, output naming, generated shell quoting, and archive enabled/disabled behavior. Keep remote network tests separate from deterministic unit tests.

## HashSuite integration contract

`verify-lfs.py` creates a temporary expected SHA-256 manifest from the selected inventory entries and invokes HashSuite's `hashcheck.py` with worker and chunk-size options. When requested, it passes `--outsha256` and `--outsha512` output destinations. HashSuite stdout and stderr are forwarded to the terminal and saved together in the per-run log.

The verifier interprets HashSuite return code 0 as checksum success, 1 as one or more checksum failures, and other nonzero values as an execution error. The verifier independently checks file size and file signatures before and after the hashing run.

Do not duplicate the digest implementation in this repository. If HashSuite's CLI contract changes, update the pinned revision record, integration code, tests, and documentation together.

## Release and packaging hygiene

- Build from a clean, known source revision and record the exact commit/revision inputs.
- Keep generated artifacts separate from source files until their contents and checksums have been inspected.
- Use relative filenames in checksum sidecars so verification does not depend on the builder's absolute home-directory path.
- Verify the checksum after creating an archive and again after copying or publishing it.
- Test extraction into a fresh directory and inspect the extracted file list.
- Do not claim a package contains files or tests that are absent from the source tree.
- Do not include local credentials, model weights, cache contents, temporary logs, or machine-specific paths in a release artifact.
