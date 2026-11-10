# Development notes

## Repository layout

- `hf-lfs-snapshot.py` — clones a Hugging Face Git repository with LFS smudging disabled, captures raw and normalized LFS inventories, and emits helper scripts.
- `verify-lfs.py` — validates inventory entries and working-tree paths, invokes external HashSuite `hashcheck.py`, checks file size/stability, and optionally prunes verified LFS cache objects.
- `lfsls.py` — reports live pointer/materialization state and maintains `local.lfsstate.json` separately from the captured inventory.
- `lfspull.sh` — static helper for pulling explicitly named paths from the current Git working tree.
- `lfspull.py` — inventory-driven serial acquisition runner with disk-space preflight, bounded retries, selectable backends, and JSON Lines run logging.
- `lfsbackends.py` — optional Hub backend adapter and acquisition result/context types.
- `lfscache.py` — read-only inventory of canonical objects in the configured Git LFS media cache.
- `tests/test_n1_regression.py` — deterministic regression tests for snapshot capture outputs and verifier safety behavior.
- `tests/test_n2_lfs_state.py` — deterministic local-state classification and cache-invalidation tests.
- `tests/test_n3_lfspull.py` — deterministic acquisition-runner tests using mocked Git commands.
- `tests/test_n4_lfscache.py` — deterministic cache-layout, inventory-correlation, and symlink-safety tests.
- `tests/test_n5_backends.py` and `tests/test_n5_hub_runner.py` — mocked Hub API and runner integration tests.
- `UPSTREAM-REVISIONS.txt` — pinned source revisions used as upstream references for the toolkit and HashSuite.

## Runtime dependencies

The snapshot tool requires Python 3.9+, Git, and Git LFS. The verifier additionally requires the separate HashSuite `hashcheck.py` script. The default acquisition backend uses the standard library; the optional `hf-hub` backend requires `huggingface_hub`. HashSuite discovery is controlled by `--hashcheck`, `HASHSUITE_HASHCHECK`, `HASHSUITE_DIR`, or `PATH`; see README for the exact precedence.

No third-party Python package is required for the default Git LFS workflow. The optional Hub backend requires `huggingface_hub`.

## Local validation

From the repository root:

```sh
python3 -m py_compile hf-lfs-snapshot.py verify-lfs.py lfsls.py lfspull.py lfsbackends.py lfscache.py
python3 -m unittest discover -s tests -v
```

The regression suite exercises:

- valid and malformed Git LFS JSON, duplicate paths, traversal paths, invalid OIDs, and invalid sizes;
- snapshot output generation, generated shell quoting for spaces/quotes/metacharacters, executable helper modes, archive default behavior, and `--no-archive`;
- normalized inventory validation and checksum-manifest filename escaping;
- symlink components and leaf symlinks, non-regular files, and missing HashSuite discovery;
- LFS pull retry behavior, disk-space preflight, size-correct skip behavior, JSONL logging, and symlink-path rejection;
- Hub exact-revision arguments, cache preservation, size validation, safe diagnostics, replacement authorization, and runner integration;
- canonical LFS cache object discovery, duplicate-OID inventory handling, cache size mismatches, and symlink non-traversal;
- verifier success/failure outcomes using a controlled HashSuite subprocess stub, including all-or-none pruning and preservation of `.git/lfs/incomplete`.

These tests mock Git clone/list operations and the HashSuite process. The N+2 state tests use temporary filesystems and do not exercise a live Hugging Face remote, actual Git LFS transfers, the real HashSuite implementation, concurrent file mutation during hashing, or platform-specific filesystem behavior. Run the real HashSuite suite separately when validating a release.

When changing verifier behavior, use a temporary Git worktree and a controlled HashSuite fixture or the pinned HashSuite checkout. Test both successful and failing checksum cases, size mismatch, missing/non-regular paths, symlinks in intermediate components, file mutation during hashing, subprocess error handling, and the all-or-none prune rule.

When changing snapshot behavior, test malformed JSON, duplicate paths, invalid OIDs and sizes, unsafe paths, output naming, generated shell quoting, and archive enabled/disabled behavior. Keep remote network tests separate from deterministic unit tests.

## Acquisition runner

`lfspull.py` reads the captured inventory, validates selected paths, and runs one `git lfs pull --include=PATH --exclude=` subprocess per path. It is serial by design. A failed command or a post-pull file that does not have the expected size is retried up to the configured retry count with capped exponential backoff. The default is two retries after the first attempt.

Before the first transfer, the runner checks free space on the worktree filesystem. Its estimate sums expected sizes for selected paths that are not already regular files of the expected size, then adds the configured reserve (1 GiB by default). It checks the worktree filesystem only and does not inspect a separately configured Git LFS media directory. It does not attempt to predict filesystem compression, sparse allocation, duplicate remote transfer behavior, or unrelated concurrent disk use; it is not a reservation or guarantee. The check can be disabled explicitly.

The JSON Lines log is appended and flushed after each event. It records run boundaries, selected paths, attempt results, bounded stdout/stderr excerpts, and final per-path outcomes. A rerun invokes Git LFS again for paths that are not already exact-size; the runner does not manipulate `.git/lfs/incomplete`, delete cache objects, or claim cryptographic verification. Exact size is only an acquisition-level observation. HashSuite verification remains a separate stage.

## Alternative acquisition backend

The default `lfspull.py` path remains Git LFS and does not import third-party packages. The optional `--backend hf-hub` path imports `lfsbackends.py` and requires `huggingface_hub`. It is limited to model repository URLs in the captured inventory and passes the full captured `git_commit` as `revision`. A missing or invalid commit is an error; there is no moving-revision fallback.

The Hub API owns authentication, transfer selection, and its cache. `--hf-cache-dir` maps to the API's `cache_dir` argument. `--disable-xet` sets `HF_HUB_DISABLE_XET=1` before the Hub download API is imported. The run-start record includes the selected backend and non-secret cache/Xet diagnostics. Hub exceptions are reduced to safe categories rather than copied verbatim into the log.

The Hub backend checks the returned cache file size, copies it to a temporary sibling under the target directory, checks the staged size, and atomically replaces the target. It does not alter the Hub cache. Existing materialized targets require explicit `--replace` authorization when a replacement is requested; `--force` alone is not replacement authorization. HashSuite verification remains separate. Unit tests mock the Hub API; they do not establish live authentication, HTTP resume, Xet transfer, or remote-cache behavior.

## LFS cache inventory

`lfscache.py` reads the `LocalMediaDir` reported by `git lfs env` unless `--cache-dir` is supplied. It scans only the `objects` subdirectory of the media directory and recognizes the canonical two-character/two-character/60-character SHA-256 object path. A symlink in place of the `objects` directory is rejected. It reports correlation with one inventory but cannot establish global reachability across other repositories sharing the cache. The command is read-only and does not inspect `.git/lfs/incomplete`.

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

## Local LFS state reporter

`lfsls.py` reads the immutable `hf-lfs-inventory-v1` inventory and observes the corresponding paths in the current Git worktree. It writes `local.lfsstate.json` beside the inventory unless `--state` overrides the destination. The state document is bound to the inventory's SHA-256 fingerprint; a changed inventory invalidates cached observations and verification evidence.

The reporter distinguishes `POINTER`, `MISSING`, `PARTIAL`, `SIZE_OK`, `VERIFIED`, `MISMATCH`, `STALE`, and `UNKNOWN`. A matching size is not proof of matching content. A `VERIFIED` result is reusable only when both the inventory fingerprint and the current file signature match the recorded verification event. Verification events are retained in per-path history.

`--verify` invokes `verify-lfs.py` separately for each selected file currently classified as `SIZE_OK` or `STALE`. It records verification outcomes but does not request or perform cache pruning. `--audit` compares current observations with saved state without writing the state file. Symlinked path components are classified as `UNKNOWN`; the reporter does not follow them to inspect files outside the worktree.
