# Changelog

This file records repository-level changes. It is not a claim that every planned feature or test has been implemented.

## N+1 documentation baseline

Documentation aligned to the published N+1 source interface:

- Describe raw `git lfs ls-files --json` capture and normalized `hf-lfs-inventory-v1` inventory.
- Document the actual snapshot outputs: `local.lfsfiles.json`, `local.lfsfiles.txt`, `lfs-inventory.json`, `clone/`, `_clone.sh`, and `local.lfspull.sh`.
- Correct the archive behavior: `.tar.gz` is created by default; `--no-archive` disables it.
- Document HashSuite as an external dependency and describe its discovery options.
- Document timestamped per-run verifier outputs, selected-path verification, file stability/size checks, and conservative batch pruning.
- State the actual regression-test scope and distinguish it from planned broader coverage.

No runtime code changes are represented by this documentation update.
