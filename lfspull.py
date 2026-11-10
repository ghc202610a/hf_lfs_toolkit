#!/usr/bin/env python3
"""Reliably acquire selected Git LFS paths from a captured inventory."""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

FORMAT = "hf-lfs-inventory-v1"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_inventory(path):
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("unsupported inventory format")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("inventory entries must be an array")
    seen = set()
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError("invalid inventory entry {}".format(number))
        rel = entry.get("path")
        if (not isinstance(rel, str) or not rel or "\x00" in rel
                or rel.startswith("/") or "\\" in rel
                or any(part in ("", ".", "..") for part in rel.split("/"))):
            raise ValueError("unsafe inventory path: {!r}".format(rel))
        oid = entry.get("oid")
        size = entry.get("size")
        if (not isinstance(oid, str) or len(oid) != 64
                or any(c not in "0123456789abcdef" for c in oid)):
            raise ValueError("invalid SHA-256 OID for {!r}".format(rel))
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("invalid size for {!r}".format(rel))
        if rel in seen:
            raise ValueError("duplicate inventory path: {!r}".format(rel))
        seen.add(rel)
    return data, entries


def path_status(root, rel, expected_size):
    path = root.joinpath(*PurePosixPath(rel).parts)
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            return "MISSING", None
        except OSError as exc:
            return "UNKNOWN", str(exc)
        if current.is_symlink():
            return "UNKNOWN", "symlink in inventory path"
        if current != path and not current.is_dir():
            return "UNKNOWN", "non-directory path component"
    try:
        st = path.lstat()
    except OSError as exc:
        return "UNKNOWN", str(exc)
    if not path.is_file():
        return "UNKNOWN", "path is not a regular file"
    try:
        with path.open("rb") as stream:
            header = stream.read(128)
    except OSError as exc:
        return "UNKNOWN", str(exc)
    if header.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        return "POINTER", None
    if st.st_size == expected_size:
        return "SIZE_OK", None
    if st.st_size < expected_size:
        return "PARTIAL", None
    return "MISMATCH", "working-tree file is larger than inventory size"


def emit(stream, record):
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def select_entries(entries, paths, patterns):
    by_path = {entry["path"]: entry for entry in entries}
    unknown = set(paths) - set(by_path)
    if unknown:
        raise ValueError("paths not present in inventory: " + ", ".join(sorted(unknown)))
    selected = [entry for entry in entries
                if (not paths or entry["path"] in paths)
                and (not patterns or any(fnmatch.fnmatchcase(entry["path"], p)
                                         for p in patterns))]
    for entry in selected:
        if any(char in entry["path"] for char in ",*?["):
            raise ValueError(
                "path contains Git LFS include-pattern syntax that cannot be "
                "passed literally: {!r}".format(entry["path"]))
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="exact inventory paths to pull")
    parser.add_argument("--inventory", default="../lfs-inventory.json")
    parser.add_argument("--include", action="append", default=[],
                        help="include paths matching this glob; repeatable")
    parser.add_argument("--retries", type=int, default=2,
                        help="retries after the first attempt (default: 2)")
    parser.add_argument("--retry-delay", type=float, default=5.0,
                        help="initial retry delay in seconds; doubles per retry")
    parser.add_argument("--reserve-gib", type=float, default=1.0,
                        help="free-space reserve retained after estimated downloads")
    parser.add_argument("--no-space-check", action="store_true",
                        help="skip the worktree free-space preflight")
    parser.add_argument("--force", action="store_true",
                        help="run git lfs pull even when the path already has expected size")
    parser.add_argument("--log", help="JSON Lines run log (default: local.lfspull.TIMESTAMP.jsonl)")
    args = parser.parse_args()

    if args.retries < 0 or args.retry_delay < 0 or not math.isfinite(args.retry_delay):
        parser.error("--retries must be nonnegative and --retry-delay finite and nonnegative")
    if args.reserve_gib < 0 or not math.isfinite(args.reserve_gib):
        parser.error("--reserve-gib must be finite and nonnegative")

    try:
        inventory_path = Path(args.inventory).expanduser().resolve()
        inventory, entries = load_inventory(inventory_path)
        selected = select_entries(entries, args.paths, args.include)
        if not selected:
            print("No inventory paths selected.")
            return 0
        root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).stdout.strip()).resolve()
        if not root.is_dir():
            raise ValueError("Git worktree root is not a directory")

        work = []
        skipped = []
        required = 0
        for entry in selected:
            status, detail = path_status(root, entry["path"], entry["size"])
            if status == "SIZE_OK" and not args.force:
                skipped.append((entry, status))
            elif status in ("UNKNOWN", "MISMATCH"):
                raise ValueError("{}: {}{}".format(
                    entry["path"], status, ": " + detail if detail else ""))
            else:
                work.append(entry)
                required += entry["size"]

        reserve = int(args.reserve_gib * (1024 ** 3))
        if not args.no_space_check:
            free = shutil.disk_usage(root).free
            needed = required + reserve
            if free < needed:
                raise ValueError(
                    "insufficient free space: {:.2f} GiB available; "
                    "{:.2f} GiB estimated transfer plus {:.2f} GiB reserve required".format(
                        free / 1024**3, required / 1024**3, reserve / 1024**3))

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = Path(args.log).expanduser() if args.log else Path("local.lfspull.{}.jsonl".format(stamp))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.is_symlink():
            raise ValueError("log destination must not be a symlink")
        resolved_log = log_path.resolve()
        if resolved_log == inventory_path:
            raise ValueError("log destination conflicts with immutable inventory")
        for entry in entries:
            target = root.joinpath(*PurePosixPath(entry["path"]).parts)
            if resolved_log == target.resolve(strict=False):
                raise ValueError("log destination conflicts with LFS working-tree path: {}".format(entry["path"]))
            if log_path.exists() and target.exists():
                try:
                    if os.path.samefile(log_path, target):
                        raise ValueError("log destination aliases LFS working-tree path: {}".format(entry["path"]))
                except OSError:
                    pass

        successes = len(skipped)
        failures = 0
        with log_path.open("a", encoding="utf-8") as log:
            emit(log, {"event": "run-start", "at_utc": now(),
                       "inventory": str(inventory_path), "repository": inventory.get("repository"),
                       "selected": len(selected), "scheduled": len(work),
                       "estimated_bytes": required, "reserve_bytes": reserve})
            for entry, status in skipped:
                print("SKIP SIZE_OK: {}".format(entry["path"]))
                emit(log, {"event": "path-skipped", "at_utc": now(),
                           "path": entry["path"], "status": status,
                           "expected_size": entry["size"],
                           "reason": "working-tree file already has expected size"})
            for entry in work:
                rel = entry["path"]
                completed = False
                for attempt in range(1, args.retries + 2):
                    print("PULL {}/{} attempt {}: {}".format(
                        successes + failures + 1, len(selected), attempt, rel), flush=True)
                    emit(log, {"event": "attempt-start", "at_utc": now(),
                               "path": rel, "attempt": attempt,
                               "expected_oid": entry["oid"],
                               "expected_size": entry["size"]})
                    try:
                        result = subprocess.run(
                            ["git", "lfs", "pull", "--include=" + rel, "--exclude="],
                            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        out = result.stdout.decode("utf-8", errors="replace")
                        err = result.stderr.decode("utf-8", errors="replace")
                    except OSError as exc:
                        result = subprocess.CompletedProcess(
                            ["git", "lfs", "pull"], 127, b"", str(exc).encode("utf-8"))
                        out = ""
                        err = str(exc)
                    if out:
                        sys.stdout.write(out)
                        sys.stdout.flush()
                    if err:
                        sys.stderr.write(err)
                        sys.stderr.flush()
                    status, detail = path_status(root, rel, entry["size"])
                    ok = result.returncode == 0 and status == "SIZE_OK"
                    emit(log, {"event": "attempt-finish", "at_utc": now(),
                               "path": rel, "attempt": attempt,
                               "exit_code": result.returncode,
                               "observed_status": status, "detail": detail,
                               "stdout": out[-12000:], "stderr": err[-12000:]})
                    if ok:
                        completed = True
                        successes += 1
                        emit(log, {"event": "path-complete", "at_utc": now(),
                                   "path": rel, "status": "SIZE_OK",
                                   "expected_size": entry["size"]})
                        print("SIZE_OK: {}".format(rel))
                        break
                    if status in ("UNKNOWN", "MISMATCH"):
                        break
                    if attempt <= args.retries:
                        delay = min(args.retry_delay * (2 ** (attempt - 1)), 300.0)
                        print("Retrying in {:.1f}s: {}".format(delay, rel), file=sys.stderr)
                        time.sleep(delay)
                if not completed:
                    failures += 1
                    emit(log, {"event": "path-failed", "at_utc": now(),
                               "path": rel, "expected_size": entry["size"]})
                    print("FAILED: {}".format(rel), file=sys.stderr)

            emit(log, {"event": "run-finish", "at_utc": now(),
                       "successes": successes, "failures": failures,
                       "selected": len(selected)})
        print("LFS pull result: {} complete/size-correct, {} failed; log: {}".format(
            successes, failures, log_path))
        return 0 if failures == 0 else 1
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
