#!/usr/bin/env python3
"""Create a pointer-only Hugging Face Git LFS snapshot and durable inventory."""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, shutil, subprocess, sys, tarfile
from pathlib import Path

OID_RE = re.compile(r"^[0-9a-f]{64}$")


def run(cmd, *, cwd=None, env=None, capture=False):
    return subprocess.run(cmd, cwd=cwd, env=env, check=True,
                          text=True, stdout=subprocess.PIPE if capture else None)


def parse_lfs_listing(text):
    rows = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        parts = raw.split(maxsplit=3)
        if len(parts) != 4:
            raise ValueError(f"cannot parse git lfs ls-files line {lineno}: {raw!r}")
        oid, marker, size_text, path = parts
        if not OID_RE.fullmatch(oid):
            raise ValueError(f"invalid LFS SHA-256 OID on line {lineno}: {oid!r}")
        if marker not in ("*", "-"):
            raise ValueError(f"unexpected LFS status marker on line {lineno}: {marker!r}")
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ValueError(f"invalid LFS size on line {lineno}: {size_text!r}") from exc
        if size < 0 or not path:
            raise ValueError(f"invalid LFS entry on line {lineno}")
        rows.append({"oid": oid, "size": size, "path": path, "listing_status": marker})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repository", help="Hugging Face Git URL, e.g. https://huggingface.co/org/model")
    ap.add_argument("--out", default=".", help="output parent directory (default: current directory)")
    ap.add_argument("--name", help="snapshot directory name (default: hf_<repo>_<timestamp>)")
    ap.add_argument("--archive", action="store_true", help="also create a .tar.gz archive")
    args = ap.parse_args()

    repo = args.repository.rstrip("/")
    repo_name = repo.rsplit("/", 1)[-1].removesuffix(".git")
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_name) or "model"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dirname = args.name or f"hf_{safe_repo}_{stamp}"
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
        run(["git", "-C", str(clone), "config", "lfs.concurrenttransfers", "1"])
        listing = run(["git", "lfs", "ls-files", "--long", "--size"], cwd=clone, capture=True).stdout
        entries = parse_lfs_listing(listing)
        inventory = {
            "format": "hf-lfs-inventory-v1",
            "repository": repo,
            "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "git_commit": run(["git", "rev-parse", "HEAD"], cwd=clone, capture=True).stdout.strip(),
            "entries": entries,
        }
        inv_path = dest / "lfs-inventory.json"
        inv_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (dest / "lfs-listing.raw.txt").write_text(listing, encoding="utf-8")
        print(f"Captured {len(entries)} LFS entries in {inv_path}")
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
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
