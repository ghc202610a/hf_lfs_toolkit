#!/usr/bin/env python3
"""Verify materialized LFS files and emit ordinary sha256sum/sha512sum manifests."""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys
from pathlib import Path
from hashcore import hash_file


def coreutils_escape(path):
    # GNU checksum format: quote filenames containing backslash/newline and
    # escape backslash/newline within the quoted name.
    s = str(path)
    if "\\" in s or "\n" in s:
        return '"' + s.replace("\\", "\\\\").replace("\n", "\\n") + '"', True
    return s, False


def write_manifest(path, rows, algorithm):
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        for filename, digest in rows:
            escaped, quoted = coreutils_escape(filename)
            prefix = "\\" if quoted else ""
            out.write(f"{prefix}{digest}  {escaped}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="specific inventory paths; default is all entries")
    ap.add_argument("--inventory", default="../lfs-inventory.json", help="captured inventory JSON")
    ap.add_argument("--sha256-out", default="SHA256SUMS", help="ordinary SHA-256 manifest output")
    ap.add_argument("--sha512-out", default="SHA512SUMS", help="ordinary SHA-512 manifest output")
    ap.add_argument("--block-size", type=int, default=8 * 1024 * 1024)
    ap.add_argument("--prune", action="store_true", help="remove corresponding verified LFS cache objects")
    args = ap.parse_args()

    inv_path = Path(args.inventory).expanduser().resolve()
    inventory = json.loads(inv_path.read_text(encoding="utf-8"))
    if inventory.get("format") != "hf-lfs-inventory-v1":
        ap.error(f"unsupported inventory format: {inventory.get('format')!r}")
    entries = inventory.get("entries", [])
    wanted = set(args.paths)
    selected = [e for e in entries if not wanted or e["path"] in wanted]
    missing_names = wanted - {e["path"] for e in selected}
    if missing_names:
        ap.error("paths not present in inventory: " + ", ".join(sorted(missing_names)))
    if not selected:
        print("No LFS entries selected.")
        return 0

    # Resolve paths relative to the Git worktree, not the inventory's directory.
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                               text=True, stdout=subprocess.PIPE).stdout.strip())
    good256, good512 = [], []
    failures = 0
    for entry in selected:
        rel = entry["path"]
        file_path = root / rel
        print(f"Checking {rel} ...", flush=True)
        try:
            if not file_path.is_file():
                raise FileNotFoundError("not a regular file or missing")
            size = file_path.stat().st_size
            if size != int(entry["size"]):
                raise ValueError(f"size mismatch: expected {entry['size']}, got {size}")
            digests = hash_file(file_path, ("sha256", "sha512"), args.block_size)
            if digests["sha256"] != entry["oid"]:
                raise ValueError(f"SHA-256 mismatch: expected {entry['oid']}, got {digests['sha256']}")
            good256.append((rel, digests["sha256"]))
            good512.append((rel, digests["sha512"]))
            print("  OK")
            if args.prune:
                # LFS object layout: .git/lfs/objects/aa/bb/<full-oid>
                git_dir = Path(subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root,
                                              check=True, text=True, stdout=subprocess.PIPE).stdout.strip())
                if not git_dir.is_absolute():
                    git_dir = root / git_dir
                obj = git_dir / "lfs" / "objects" / entry["oid"][:2] / entry["oid"][2:4] / entry["oid"]
                if obj.is_file():
                    obj.unlink()
                    print("  Removed verified local LFS cache object")
        except Exception as exc:
            failures += 1
            print(f"  FAIL: {exc}", file=sys.stderr)

    if good256:
        write_manifest(args.sha256_out, good256, "sha256")
        write_manifest(args.sha512_out, good512, "sha512")
        print(f"Wrote {args.sha256_out} and {args.sha512_out} ({len(good256)} verified files)")
    print(f"Result: {len(good256)} verified, {failures} failed")
    return 1 if failures else 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
