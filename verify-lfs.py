#!/usr/bin/env python3
"""Verify Git LFS files using the installed HashSuite hashcheck.py."""
from __future__ import annotations

import argparse
import datetime as dt
import time
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


def checksum_line(oid, filename):
    """Encode a GNU-compatible SHA-256 text-mode checksum record."""
    name = os.fsencode(filename)
    escaped = any(ch in name for ch in (b"\\", b"\n", b"\r"))

    if escaped:
        name = (
            name.replace(b"\\", b"\\\\")
                .replace(b"\n", b"\\n")
                .replace(b"\r", b"\\r")
        )

    return (
        (b"\\" if escaped else b"")
        + oid.encode("ascii")
        + b"  "
        + name
        + b"\n"
    )


def atomic_write(path, data):
    """Write a file through a temporary sibling and atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )

    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())

        os.replace(tmp, path)

    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_hashcheck(explicit):
    candidate = (
        explicit
        or os.environ.get("HASHSUITE_HASHCHECK")
    )

    if candidate:
        expanded = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(expanded):
            return expanded

        found = shutil.which(candidate)
        if found:
            return found

        raise FileNotFoundError(
            f"HashSuite hashcheck.py not found: {candidate!r}"
        )

    suite_dir = os.environ.get("HASHSUITE_DIR")
    if suite_dir:
        path = (
            Path(suite_dir).expanduser()
            / "hashcheck.py"
        )

        if path.is_file():
            return str(path.resolve())

        raise FileNotFoundError(
            f"HASHSUITE_DIR does not contain hashcheck.py: {suite_dir}"
        )

    found = shutil.which("hashcheck.py")
    if found:
        return found

    raise FileNotFoundError(
        "HashSuite hashcheck.py not found. Use --hashcheck, "
        "HASHSUITE_HASHCHECK, HASHSUITE_DIR, or PATH."
    )


def same_file(path1, path2):
    if os.path.abspath(path1) == os.path.abspath(path2):
        return True

    try:
        return os.path.samefile(path1, path2)
    except (OSError, ValueError):
        return os.path.realpath(path1) == os.path.realpath(path2)


def safe_relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"invalid LFS path: {value!r}")

    path = PurePosixPath(value)

    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe LFS path: {value!r}")

    return value


def load_inventory(path):
    inventory = json.loads(path.read_text(encoding="utf-8"))

    if inventory.get("format") != "hf-lfs-inventory-v1":
        raise ValueError(
            f"unsupported inventory format: {inventory.get('format')!r}"
        )

    entries = inventory.get("entries")
    if not isinstance(entries, list):
        raise ValueError("inventory entries must be an array")

    seen = set()

    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"invalid inventory entry {index}")

        rel = safe_relative_path(entry.get("path"))
        oid = entry.get("oid")
        size = entry.get("size")

        if (
            not isinstance(oid, str)
            or len(oid) != 64
            or any(c not in "0123456789abcdef" for c in oid)
        ):
            raise ValueError(f"invalid SHA-256 OID for {rel!r}")

        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid size for {rel!r}: {size!r}")

        if rel in seen:
            raise ValueError(f"duplicate inventory path: {rel!r}")

        seen.add(rel)

    return inventory, entries


def reject_symlink_path(root, path):
    rel = path.relative_to(root)
    current = root
    for component in rel.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlink in inventory path: {current}")


def file_signature(path):
    """Return stable file identity and size information."""
    try:
        st = path.stat()
    except OSError:
        return None

    if not stat.S_ISREG(st.st_mode):
        return ("not-regular", st.st_mode)

    return (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)

    ap.add_argument(
        "paths",
        nargs="*",
        help="specific inventory paths; default is all entries",
    )
    ap.add_argument(
        "--inventory",
        default="../lfs-inventory.json",
    )
    ap.add_argument(
        "--hashcheck",
        help="path to HashSuite hashcheck.py",
    )
    ap.add_argument(
        "--manifest-out",
        default=None,
        help="authoritative expected SHA-256 manifest",
    )
    ap.add_argument(
        "--sha256-out",
        help="optional computed SHA-256 inventory",
    )
    ap.add_argument(
        "--sha512-out",
        default=None,
        help="computed SHA-512 inventory",
    )
    ap.add_argument(
        "--log-out",
        default=None,
        help="HashSuite verification output",
    )
    ap.add_argument("-j", "--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    ap.add_argument("--chunk-mib", type=int, default=8)
    ap.add_argument("--no-advice", action="store_true")
    ap.add_argument(
        "--prune",
        action="store_true",
        help="remove verified LFS cache objects after successful verification",
    )

    ap.add_argument(
        "--run-id",
        help="output identifier; default UTC timestamp and Unix time",
    )
    args = ap.parse_args()

    run_id = args.run_id or (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_")
        + str(int(time.time()))
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        ap.error("invalid --run-id")

    args.manifest_out = (
        args.manifest_out or f"local.sha256sums.check.{run_id}.txt"
    )
    args.sha256_out = (
        args.sha256_out or f"local.sha256sums.{run_id}.txt"
    )
    args.sha512_out = (
        args.sha512_out or f"local.sha256sums.sha512.{run_id}.txt"
    )
    args.log_out = (
        args.log_out or f"local.hashcheck.{run_id}.log"
    )

    if args.jobs < 1 or args.chunk_mib < 1:
        ap.error("--jobs and --chunk-mib must be positive")

    try:
        root = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()
        ).resolve()
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: cannot locate Git worktree: {exc}",
            file=sys.stderr,
        )
        return 2

    inv_path = Path(args.inventory).expanduser().resolve()
    inventory, entries = load_inventory(inv_path)

    by_path = {entry["path"]: entry for entry in entries}
    wanted = set(args.paths)

    unknown = wanted - set(by_path)
    if unknown:
        ap.error(
            "paths not present in inventory: "
            + ", ".join(sorted(unknown))
        )

    selected = [
        entry for entry in entries
        if not wanted or entry["path"] in wanted
    ]

    manifest_out = Path(args.manifest_out).expanduser().resolve()
    sha256_out = (
        Path(args.sha256_out).expanduser().resolve()
        if args.sha256_out else None
    )
    sha512_out = (
        Path(args.sha512_out).expanduser().resolve()
        if args.sha512_out else None
    )
    log_out = (
        Path(args.log_out).expanduser().resolve()
        if args.log_out else None
    )

    outputs = [
        p for p in (manifest_out, sha256_out, sha512_out, log_out)
        if p is not None
    ]

    for index, output in enumerate(outputs):
        if same_file(output, inv_path):
            ap.error(f"output conflicts with inventory: {output}")

        for previous in outputs[:index]:
            if same_file(output, previous):
                ap.error(f"output destinations alias: {output}")

        for entry in entries:
            working_file = root.joinpath(
                *PurePosixPath(entry["path"]).parts
            )

            if same_file(output, working_file):
                ap.error(
                    f"output conflicts with LFS working-tree file: {output}"
                )

    # The authoritative manifest describes the complete captured inventory.
    manifest_data = b"".join(
        checksum_line(entry["oid"], entry["path"])
        for entry in entries
    )

    atomic_write(manifest_out, manifest_data)

    print(
        f"Wrote authoritative LFS SHA-256 manifest: "
        f"{manifest_out} ({len(entries)} entries)"
    )

    if not selected:
        print("No LFS entries selected.")
        return 0

    try:
        hashcheck = resolve_hashcheck(args.hashcheck)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    before = {}

    for entry in selected:
        path = root.joinpath(*PurePosixPath(entry["path"]).parts)

        try:
            reject_symlink_path(root, path)
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(root):
                raise ValueError(
                    f"LFS path resolves outside worktree: {entry['path']}"
                )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        before[entry["path"]] = file_signature(path)

    with tempfile.TemporaryDirectory(prefix="hf-lfs-hashcheck-") as temp:
        expected = Path(temp) / "expected.sha256"
        expected.write_bytes(
            b"".join(
                checksum_line(entry["oid"], entry["path"])
                for entry in selected
            )
        )

        command = [
            sys.executable,
            hashcheck,
            "--jobs", str(args.jobs),
            "--chunk-mib", str(args.chunk_mib),
        ]

        if args.no_advice:
            command.append("--no-advice")

        if sha256_out:
            command.extend(["--outsha256", str(sha256_out)])

        if sha512_out:
            command.extend(["--outsha512", str(sha512_out)])

        command.append(str(expected))

        result = subprocess.run(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if result.stdout:
            sys.stdout.buffer.write(result.stdout)
            sys.stdout.buffer.flush()

        if result.stderr:
            sys.stderr.buffer.write(result.stderr)
            sys.stderr.buffer.flush()

        if log_out:
            log_data = (
                b"===== HashSuite stdout =====\n"
                + result.stdout
                + b"\n===== HashSuite stderr =====\n"
                + result.stderr
            )
            atomic_write(log_out, log_data)
            print(f"Saved combined verification log: {log_out}")

    if result.returncode not in (0, 1):
        print(
            f"ERROR: HashSuite exited with status {result.returncode}",
            file=sys.stderr,
        )
        return 2

    # HashSuite returns zero only if every expected checksum passed.
    # Conservatively prune nothing if any checksum failed.
    hash_ok = (
        {entry["path"] for entry in selected}
        if result.returncode == 0
        else set()
    )

    size_ok = set()
    failures = []

    for entry in selected:
        rel = entry["path"]
        path = root.joinpath(*PurePosixPath(rel).parts)

        initial = before[rel]
        after = file_signature(path)
        expected_size = entry["size"]

        if initial is None or after is None:
            failures.append((rel, "missing or inaccessible"))
            continue

        if initial[0] == "not-regular" or after[0] == "not-regular":
            failures.append((rel, "not a regular file"))
            continue

        if initial[2] != expected_size or after[2] != expected_size:
            failures.append((
                rel,
                f"size mismatch: expected {expected_size}, "
                f"before {initial[2]}, after {after[2]}",
            ))
            continue

        if initial != after:
            failures.append((rel, "file changed while HashSuite was reading it"))
            continue

        size_ok.add(rel)

    verified = hash_ok & size_ok

    for rel, reason in failures:
        print(f"SIZE/FILE FAIL: {rel}: {reason}", file=sys.stderr)

    if args.prune and len(verified) == len(selected):
        try:
            git_dir_text = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()

            git_dir = Path(git_dir_text)
            if not git_dir.is_absolute():
                git_dir = root / git_dir
            git_dir = git_dir.resolve()

            removed = set()

            for entry in selected:
                oid = entry["oid"]
                if oid in removed:
                    continue

                obj = (
                    git_dir / "lfs" / "objects"
                    / oid[:2] / oid[2:4] / oid
                )

                if obj.is_file():
                    obj.unlink()
                    removed.add(oid)
                    print(f"Pruned verified LFS object: {oid}")

        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"ERROR: LFS pruning failed: {exc}", file=sys.stderr)
            return 1

    elif args.prune:
        print(
            "Pruning skipped: at least one selected file did not pass "
            "the complete verification and size checks.",
            file=sys.stderr,
        )

    failed = len(selected) - len(verified)

    print(
        f"LFS result: {len(verified)} verified, {failed} failed"
    )

    return 0 if failed == 0 and result.returncode == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
