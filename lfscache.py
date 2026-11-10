#!/usr/bin/env python3
"""Read-only inventory of Git LFS objects in the configured local media cache."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


OID_RE = re.compile(r"^[0-9a-f]{64}$")


def load_inventory(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "hf-lfs-inventory-v1":
        raise ValueError(f"unsupported inventory format: {data.get('format')!r}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("inventory entries must be an array")
    oids = {}
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"invalid inventory entry {index}")
        oid = entry.get("oid")
        size = entry.get("size")
        path = entry.get("path")
        if not isinstance(oid, str) or not OID_RE.fullmatch(oid):
            raise ValueError(f"invalid SHA-256 OID in inventory entry {index}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid size in inventory entry {index}")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError(f"invalid path in inventory entry {index}")
        oids.setdefault(oid, {"expected_size": size, "paths": []})
        if oids[oid]["expected_size"] != size:
            raise ValueError(f"conflicting sizes for LFS OID {oid}")
        oids[oid]["paths"].append(path)
    return oids


def discover_media_dir(root):
    result = subprocess.run(
        ["git", "lfs", "env"], cwd=root, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "LocalMediaDir":
            value = value.strip()
            if value:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = root / path
                return path.resolve()
    raise ValueError("git lfs env did not report LocalMediaDir")


def object_oid(relative):
    parts = relative.parts
    if len(parts) != 3:
        return None
    if len(parts[0]) != 2 or len(parts[1]) != 2 or len(parts[2]) != 60:
        return None
    oid = "".join(parts)
    if not OID_RE.fullmatch(oid):
        return None
    return oid


def scan_cache(cache_dir):
    objects = []
    skipped_symlinks = 0
    unreadable = 0
    objects_dir = cache_dir / "objects"
    if not objects_dir.exists():
        return objects, skipped_symlinks, unreadable
    try:
        objects_mode = objects_dir.lstat().st_mode
    except FileNotFoundError:
        return objects, skipped_symlinks, unreadable
    if not stat.S_ISDIR(objects_mode):
        raise ValueError(f"Git LFS objects path is not a real directory (symlinks are not followed): {objects_dir}")

    def walk(directory, relative):
        nonlocal skipped_symlinks, unreadable
        try:
            entries = list(os.scandir(directory))
        except OSError:
            unreadable += 1
            return
        for item in entries:
            rel = relative / item.name
            try:
                mode = item.stat(follow_symlinks=False).st_mode
            except OSError:
                unreadable += 1
                continue
            if stat.S_ISLNK(mode):
                skipped_symlinks += 1
                continue
            if stat.S_ISDIR(mode):
                walk(Path(item.path), rel)
                continue
            if not stat.S_ISREG(mode):
                continue
            oid = object_oid(rel)
            if oid is not None:
                objects.append({
                    "oid": oid,
                    "path": str(Path(item.path)),
                    "size": item.stat(follow_symlinks=False).st_size,
                })
    walk(objects_dir, Path())
    objects.sort(key=lambda item: item["oid"])
    return objects, skipped_symlinks, unreadable


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", default="../lfs-inventory.json")
    ap.add_argument("--cache-dir", help="override the LocalMediaDir reported by git lfs env")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--show-objects", action="store_true", help="include per-object rows")
    args = ap.parse_args(argv)

    try:
        root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()).resolve()
        inventory = load_inventory(args.inventory)
        cache_dir = (
            Path(args.cache_dir).expanduser().resolve()
            if args.cache_dir else discover_media_dir(root)
        )
        objects, skipped_symlinks, unreadable = scan_cache(cache_dir)
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    referenced = set(inventory)
    present = {item["oid"] for item in objects}
    known = [item for item in objects if item["oid"] in referenced]
    not_in_inventory = [item for item in objects if item["oid"] not in referenced]
    missing = sorted(referenced - present)
    size_mismatches = [
        {"oid": item["oid"], "cached_size": item["size"],
         "expected_size": inventory[item["oid"]]["expected_size"]}
        for item in known
        if item["size"] != inventory[item["oid"]]["expected_size"]
    ]
    summary = {
        "format": "hf-lfs-cache-report-v1",
        "cache_dir": str(cache_dir),
        "inventory_oid_count": len(referenced),
        "cache_object_count": len(objects),
        "cache_bytes": sum(item["size"] for item in objects),
        "referenced_object_count": len(known),
        "referenced_bytes": sum(item["size"] for item in known),
        "not_in_inventory_count": len(not_in_inventory),
        "not_in_inventory_bytes": sum(item["size"] for item in not_in_inventory),
        "inventory_objects_missing_from_cache": missing,
        "size_mismatches": size_mismatches,
        "skipped_symlink_count": skipped_symlinks,
        "unreadable_count": unreadable,
        "objects": objects if args.show_objects else None,
        "not_in_inventory_objects": not_in_inventory if args.show_objects else None,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Git LFS cache: {cache_dir}")
        print(f"Inventory OIDs: {len(referenced)}")
        print(f"Cache objects: {len(objects)} ({summary['cache_bytes']} bytes)")
        print(f"Referenced by this inventory: {len(known)} ({summary['referenced_bytes']} bytes)")
        print(f"Not referenced by this inventory: {len(not_in_inventory)} ({summary['not_in_inventory_bytes']} bytes)")
        print(f"Inventory OIDs absent from cache: {len(missing)}")
        print(f"Cached object size mismatches: {len(size_mismatches)}")
        print(f"Skipped symlinks: {skipped_symlinks}; unreadable entries/directories: {unreadable}")
        if size_mismatches:
            for item in size_mismatches:
                print(f"SIZE MISMATCH {item['oid']}: cache={item['cached_size']} inventory={item['expected_size']}")
        if args.show_objects:
            for item in objects:
                relation = "inventory" if item["oid"] in referenced else "not-in-inventory"
                print(f"{relation:16} {item['size']:>14} {item['oid']}  {item['path']}")
        print("No cache objects were modified. Objects absent from this inventory may be used by other repositories.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
