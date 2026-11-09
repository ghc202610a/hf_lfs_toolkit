#!/usr/bin/env python3
"""Create a pointer-only Hugging Face Git LFS snapshot."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

OID_RE = re.compile(r"^[0-9a-f]{64}$")
LFS_VERSION = "https://git-lfs.github.com/spec/v1"


def run(cmd, *, cwd=None, env=None, capture=False):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else None


def parse_lfs_json(raw):
    """Validate and normalize git-lfs ls-files --json output."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Git LFS JSON: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError("expected a JSON object containing a 'files' array")

    entries = []
    seen = set()

    for index, item in enumerate(payload["files"], 1):
        if not isinstance(item, dict):
            raise ValueError(f"invalid LFS entry {index}: expected object")

        name = item.get("name")
        oid_type = item.get("oid_type")
        oid = item.get("oid")
        size = item.get("size")
        version = item.get("version")

        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError(f"invalid LFS path in entry {index}: {name!r}")

        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe LFS path: {name!r}")

        if name in seen:
            raise ValueError(f"duplicate LFS path: {name!r}")
        seen.add(name)

        if oid_type != "sha256" or not isinstance(oid, str) or not OID_RE.fullmatch(oid):
            raise ValueError(f"invalid SHA-256 OID for {name!r}")

        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid size for {name!r}: {size!r}")

        if version != LFS_VERSION:
            raise ValueError(f"unsupported LFS pointer version for {name!r}")

        entry = {
            "path": name,
            "oid": oid,
            "size": size,
            "oid_type": oid_type,
            "version": version,
        }

        for key in ("checkout", "downloaded"):
            if key in item:
                entry[key] = item[key]

        entries.append(entry)

    return payload, entries


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "repository",
        help="Hugging Face Git URL, e.g. https://huggingface.co/org/model",
    )
    ap.add_argument("--out", default=".", help="output parent directory")
    ap.add_argument("--name", help="snapshot directory name")
    ap.add_argument("--archive", action="store_true", help="also create .tar.gz")
    args = ap.parse_args()

    repo = args.repository.rstrip("/")
    repo_name = repo.rsplit("/", 1)[-1].removesuffix(".git")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_name) or "model"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dirname = args.name or f"hf_{safe_name}_{stamp}"

    parent = Path(args.out).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)

    dest = parent / dirname
    if dest.exists():
        ap.error(f"destination already exists: {dest}")
    dest.mkdir()

    clone = dest / "clone"
    env = os.environ.copy()
    env["GIT_LFS_SKIP_SMUDGE"] = "1"

    try:
        print(f"Cloning pointer-only repository: {repo}")
        run(["git", "clone", repo, str(clone)], env=env)

        raw_json = run(
            ["git", "lfs", "ls-files", "--json"],
            cwd=clone,
            capture=True,
        )
        _, entries = parse_lfs_json(raw_json)

        inventory = {
            "format": "hf-lfs-inventory-v1",
            "repository": repo,
            "captured_at_utc": dt.datetime.now(
                dt.timezone.utc
            ).isoformat(),
            "git_commit": run(
                ["git", "rev-parse", "HEAD"],
                cwd=clone,
                capture=True,
            ).decode().strip(),
            "source": "git lfs ls-files --json",
            "entries": entries,
        }

        (dest / "local.lfsfiles.json").write_bytes(raw_json)

        (dest / "lfs-inventory.json").write_text(
            json.dumps(inventory, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        print(f"Captured {len(entries)} LFS entries.")
        print(f"Raw JSON: {dest / 'local.lfsfiles.json'}")
        print(f"Inventory: {dest / 'lfs-inventory.json'}")
        print(f"Pointer-only clone: {clone}")

        if args.archive:
            archive = parent / f"{dirname}.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(dest, arcname=dirname)
            print(f"Archive: {archive}")

    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"ERROR: {exc}")
