# Roadmap

This is a planning document. Items below are proposed follow-on work, not statements about functionality currently present in `main`.

## Current baseline (N+1)

- Capture pointer-only Hugging Face Git working tree.
- Preserve raw Git LFS JSON and a normalized `hf-lfs-inventory-v1` inventory.
- Emit a human-readable LFS listing and helper scripts for clone recreation and per-path pulls.
- Verify materialized files against captured SHA-256 OIDs and byte sizes through external HashSuite.
- Emit timestamped per-run expected/computed manifests and a combined HashSuite log.
- Offer conservative all-or-none pruning of verified LFS cache objects.

## N+2 — LFS listing and state report

- Add a dedicated `lfsls.py` presentation/reporting utility.
- Define `local.lfsstate.json` as a machine-readable view of current local LFS state.
- Distinguish pointer-present, missing, partial, size-correct, verified, mismatched, stale, and unresolved states without conflating acquisition with verification.
- Bind cached verification evidence to both the exact inventory fingerprint and the observed file signature; retain verification events separately from the current observation.
- Keep the state report regenerable and separate from immutable snapshot inventory metadata.

## N+3 — Reliable acquisition and operational preflight

- Add `lfspull.py`, an inventory-driven serial pull runner.
- Add bounded per-path retries with exponential backoff.
- Add worktree free-space preflight with a configurable reserve and explicit opt-out.
- Add durable JSON Lines run/attempt/path outcome logging.
- Skip already exact-size regular files by default while keeping size equality distinct from cryptographic verification.
- Delegate incomplete-transfer and cache handling to Git LFS; never delete `.git/lfs/incomplete`.
- Keep verification and pruning in their existing separate tools/stages.

## N+4 — LFS cache inspection and cleanup

- Inventory the local LFS cache before proposing cleanup.
- Preserve `.git/lfs/incomplete` by default.
- If stale temporary-file cleanup is added, require conservative age/process checks, dry-run support, and clear reporting.
- Consider filesystem-specific optimizations only as optional features.

## N+5 — Alternative acquisition backend evaluation

- Evaluate direct-download and resumable-transfer backends, including authentication behavior and compatibility with the target hosting service.
- Compare tools such as aria2 only where they provide a measurable benefit.
- Keep acquisition backends behind a clear interface; retain Git LFS as a supported baseline.

## N+6 — Snapshot comparison and incremental plans

- Compare two captured inventories.
- Identify added, removed, changed, and unchanged paths by path, OID, and size.
- Generate an incremental acquisition plan without modifying source snapshots.

## N+7 — Xet storage/reuse evaluation

- Investigate whether Xet-backed repositories expose useful chunk reuse or delta reconstruction opportunities.
- Keep conclusions evidence-based and separate from the current Git LFS object model.
- Do not make assumptions that LFS OIDs map directly to Xet chunk identities.

## Cross-cutting design constraints

- Keep snapshot metadata durable and self-describing: source URL, exact commit, capture timestamp, raw listing, per-file OID/size, and tool/revision information.
- Distinguish download completion from cryptographic verification.
- Treat verification results as explicit events or run records rather than overwriting prior evidence.
- Keep acquisition, verification, cataloging, storage relocation, and deployment as separate layers.
- Model future movement of model data (for example, incoming to frozen/archive storage) as explicit state transitions.
- Avoid embedding machine-specific paths or credentials in portable metadata and release artifacts.
