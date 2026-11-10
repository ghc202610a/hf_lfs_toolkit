# Repository instructions for coding agents

## Project purpose

Maintain a small, auditable toolkit for capturing Hugging Face Git LFS metadata, materializing selected files, and verifying materialized bytes against the captured LFS SHA-256 OIDs and sizes.

## Source-of-truth rules

- Treat the checked-in Python and shell scripts as the authoritative interface. Do not infer implemented behavior from roadmap items or older documentation.
- Keep the normalized inventory format identifier `hf-lfs-inventory-v1` stable unless a deliberate format migration is proposed and documented.
- Preserve the raw `git lfs ls-files --json` capture alongside the normalized inventory.
- HashSuite is the hashing engine. Do not add a parallel hashing implementation to this repository or modify HashSuite source as part of a toolkit change.
- Keep acquisition, inventory capture, verification, cache management, and any future catalog/deployment functions conceptually separate.
- Do not embed credentials, tokens, or machine-specific paths in source, generated scripts, documentation examples, or committed artifacts.

## Safety and correctness requirements

- Treat repository paths and JSON received from Git/Git LFS as untrusted input. Validate path traversal, duplicate entries, OID type/format, sizes, and expected pointer version.
- Verification must reject symlinks in every path component and must not follow an inventory path outside the worktree.
- Require regular files and exact expected sizes. Detect file mutation during hashing where practical.
- Do not prune any LFS cache object unless the complete selected batch has passed SHA-256 and size/stability checks.
- Never remove `.git/lfs/incomplete` as part of cache pruning.
- Generated scripts must quote path and repository arguments safely and must refuse destructive overwrite of an existing clone destination.
- Avoid silently changing the meaning of existing CLI flags or output formats.

## Change discipline

- Keep changes scoped. Do not bundle unrelated refactors, dependency changes, or HashSuite modifications into a toolkit fix.
- Update README and CHANGELOG when user-visible behavior or CLI interfaces change.
- Update DEVELOPMENT.md when test or packaging procedures change, and ROADMAP.md when milestone scope changes.
- Add regression tests for every corrected defect. Do not describe a behavior as tested unless a test actually exercises it.
- Preserve executable mode on the command-line scripts. Python test modules do not need executable mode unless they have a shebang and are intended to be run directly.
- Do not commit generated model data, credentials, local caches, or machine-specific build paths.

## Validation before proposing a change

At minimum, run:

```sh
python3 -m py_compile hf-lfs-snapshot.py verify-lfs.py
python3 -m unittest discover -s tests -v
```

For verifier changes, also exercise the relevant HashSuite integration using a controlled test fixture. For snapshot changes, test parsing and generated script contents without requiring a live remote clone where possible. Report commands run, outcomes, and any skipped integration tests accurately.
