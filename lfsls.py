#!/usr/bin/env python3
"""Report local Git LFS materialization and verification state."""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

INVENTORY_FORMAT = "hf-lfs-inventory-v1"
STATE_FORMAT = "hf-lfs-state-v1"
STATUSES = ("POINTER", "MISSING", "PARTIAL", "SIZE_OK",
            "VERIFIED", "MISMATCH", "STALE", "UNKNOWN")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid inventory path: {!r}".format(value))
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("unsafe inventory path: {!r}".format(value))
    return value


def load_inventory(path):
    raw = path.read_bytes()
    inventory = json.loads(raw.decode("utf-8"))
    if not isinstance(inventory, dict) or inventory.get("format") != INVENTORY_FORMAT:
        raise ValueError("unsupported inventory format")
    entries = inventory.get("entries")
    if not isinstance(entries, list):
        raise ValueError("inventory entries must be an array")
    seen = set()
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError("invalid inventory entry {}".format(number))
        rel = safe_relative_path(entry.get("path"))
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
    return inventory, entries, hashlib.sha256(raw).hexdigest()


def read_state(path, inventory_digest):
    if not path.exists():
        return {"format": STATE_FORMAT, "inventory_sha256": inventory_digest,
                "updated_at_utc": None, "entries": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("format") != STATE_FORMAT:
        raise ValueError("unsupported local state format")
    if state.get("inventory_sha256") != inventory_digest:
        return {"format": STATE_FORMAT, "inventory_sha256": inventory_digest,
                "updated_at_utc": None, "entries": {}}
    entries = state.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("local state entries must be an object")
    return state


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".",
                                     dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def signature(path):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None, "MISSING", None
    except OSError as exc:
        return None, "UNKNOWN", str(exc)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None, "UNKNOWN", "not a regular file or is a symlink"
    return {
        "device": st.st_dev, "inode": st.st_ino, "size": st.st_size,
        "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
    }, None, None


def pointer_metadata(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(4096)
    except OSError as exc:
        return {"error": str(exc)}
    if not data.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        return None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return {"invalid": True}
    oid_match = re.search(r"(?m)^oid sha256:([0-9a-f]{64})$", text)
    size_match = re.search(r"(?m)^size ([0-9]+)$", text)
    if not oid_match or not size_match:
        return {"invalid": True}
    return {"oid": oid_match.group(1), "size": int(size_match.group(1))}


def worktree_path_problem(root, path):
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "UNKNOWN", "path is outside the Git worktree"
    current = root
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return "MISSING", None
        except OSError as exc:
            return "UNKNOWN", str(exc)
        if stat.S_ISLNK(mode):
            return "UNKNOWN", "symlink in inventory path: {}".format(current)
    try:
        if not path.resolve(strict=True).is_relative_to(root):
            return "UNKNOWN", "path resolves outside the Git worktree"
    except FileNotFoundError:
        return "MISSING", None
    except OSError as exc:
        return "UNKNOWN", str(exc)
    return None, None


def observe(root, entry, previous, inventory_digest):
    rel = entry["path"]
    path = root.joinpath(*PurePosixPath(rel).parts)
    problem_status, problem = worktree_path_problem(root, path)
    if problem_status:
        history = previous.get("verification_history", []) if isinstance(previous, dict) else []
        observed = {"path": rel, "observed_at_utc": utc_now(),
                    "file_signature": None,
                    "verification_history": history if isinstance(history, list) else [],
                    "status": problem_status}
        if problem:
            observed["detail"] = problem
        return observed
    sig, error_status, error = signature(path)
    history = previous.get("verification_history", []) if isinstance(previous, dict) else []
    if not isinstance(history, list):
        history = []
    observed = {"path": rel, "observed_at_utc": utc_now(),
                "file_signature": sig, "verification_history": history}
    if error_status:
        observed["status"] = error_status
        if error:
            observed["detail"] = error
        return observed

    pointer = pointer_metadata(path)
    if pointer is not None:
        if pointer.get("error"):
            observed["status"] = "UNKNOWN"
            observed["detail"] = "cannot inspect file content: {}".format(pointer["error"])
            return observed
        if pointer.get("invalid") or pointer.get("oid") != entry["oid"] or pointer.get("size") != entry["size"]:
            observed["status"] = "MISMATCH"
            observed["detail"] = "LFS pointer metadata differs from captured inventory"
        else:
            observed["status"] = "POINTER"
        return observed

    actual_size = sig["size"]
    expected_size = entry["size"]
    if actual_size < expected_size:
        observed["status"] = "PARTIAL"
        observed["detail"] = "file is smaller than expected ({} < {})".format(actual_size, expected_size)
        return observed
    if actual_size > expected_size:
        observed["status"] = "MISMATCH"
        observed["detail"] = "file is larger than expected ({} > {})".format(actual_size, expected_size)
        return observed

    relevant = [event for event in history
                if isinstance(event, dict) and event.get("status") in ("VERIFIED", "MISMATCH")]
    matching = [event for event in relevant
                if event.get("inventory_sha256") == inventory_digest
                and event.get("file_signature") == sig]
    if matching:
        observed["status"] = matching[-1]["status"]
        return observed
    if relevant:
        observed["status"] = "STALE"
        observed["detail"] = "file signature or inventory changed since verification"
        return observed

    observed["status"] = "SIZE_OK"
    return observed


def run_verification(root, inventory_path, entry, args, verifier_path):
    rel = entry["path"]
    with tempfile.TemporaryDirectory(prefix="lfsls-verify-") as td:
        temp = Path(td)
        command = [
            sys.executable, str(verifier_path),
            "--inventory", str(inventory_path),
            "--run-id", "lfsls",
            "--manifest-out", str(temp / "expected.txt"),
            "--sha256-out", str(temp / "computed256.txt"),
            "--sha512-out", str(temp / "computed512.txt"),
            "--log-out", str(temp / "hash.log"),
            "--jobs", str(args.jobs),
            "--chunk-mib", str(args.chunk_mib),
        ]
        if args.hashcheck:
            command.extend(["--hashcheck", args.hashcheck])
        if args.no_advice:
            command.append("--no-advice")
        command.append(rel)
        result = subprocess.run(command, cwd=str(root),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        output = result.stdout + b"\n" + result.stderr
        sig, err_status, error = signature(root.joinpath(*PurePosixPath(rel).parts))
        if result.returncode == 0:
            status = "VERIFIED"
        elif result.returncode == 1 and b"SIZE/FILE FAIL:" not in output:
            status = "MISMATCH"
        else:
            status = "ERROR"
        return {
            "status": status,
            "verified_at_utc": utc_now(),
            "inventory_sha256": args.inventory_digest,
            "file_signature": sig,
            "exit_code": result.returncode,
            "detail": output.decode("utf-8", errors="replace")[-4000:],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="exact inventory paths to report")
    parser.add_argument("--inventory", default="../lfs-inventory.json")
    parser.add_argument("--state", help="state JSON path (default: beside inventory)")
    parser.add_argument("--status", help="comma-separated status filter")
    parser.add_argument("--include", action="append", default=[],
                        help="include paths matching this glob; repeatable")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--audit", action="store_true",
                        help="compare current observations with saved state without writing")
    parser.add_argument("--verify", action="store_true",
                        help="verify eligible selected files with verify-lfs.py")
    parser.add_argument("--hashcheck", help="HashSuite hashcheck.py path")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--chunk-mib", type=int, default=8)
    parser.add_argument("--no-advice", action="store_true")
    args = parser.parse_args()

    if args.audit and args.verify:
        parser.error("--audit and --verify cannot be used together")
    if args.jobs < 1 or args.chunk_mib < 1:
        parser.error("--jobs and --chunk-mib must be positive")
    try:
        inventory_path = Path(args.inventory).expanduser().resolve()
        inventory, entries, digest = load_inventory(inventory_path)
        args.inventory_digest = digest
        state_candidate = (Path(args.state).expanduser() if args.state
                           else inventory_path.parent / "local.lfsstate.json")
        if state_candidate.is_symlink():
            raise ValueError("state destination must not be a symlink")
        state_path = state_candidate.resolve()
        root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).stdout.strip()).resolve()
        if state_path == inventory_path:
            raise ValueError("state destination conflicts with immutable inventory")
        if state_path.exists() and inventory_path.exists():
            if os.path.samefile(state_path, inventory_path):
                raise ValueError("state destination aliases immutable inventory")
        for item in entries:
            working_file = root.joinpath(*PurePosixPath(item["path"]).parts)
            if state_path == working_file.resolve(strict=False):
                raise ValueError("state destination conflicts with LFS working-tree path: {}".format(item["path"]))
            if state_path.exists() and working_file.exists():
                try:
                    if os.path.samefile(state_path, working_file):
                        raise ValueError("state destination aliases LFS working-tree path: {}".format(item["path"]))
                except OSError:
                    pass
        saved = read_state(state_path, digest)
        previous_entries = saved["entries"]

        by_path = {item["path"]: item for item in entries}
        unknown = set(args.paths) - set(by_path)
        if unknown:
            parser.error("paths not present in inventory: " + ", ".join(sorted(unknown)))
        selected = [item for item in entries
                    if (not args.paths or item["path"] in args.paths)
                    and (not args.include or any(fnmatch.fnmatchcase(item["path"], p)
                                                 for p in args.include))]
        status_filter = None
        if args.status:
            status_filter = {part.strip().upper() for part in args.status.split(",") if part.strip()}
            invalid = status_filter - set(STATUSES)
            if invalid:
                parser.error("invalid --status value(s): " + ", ".join(sorted(invalid)))

        current = {}
        for item in selected:
            rel = item["path"]
            current[rel] = observe(root, item, previous_entries.get(rel, {}), digest)

        if args.verify:
            verifier_path = Path(__file__).with_name("verify-lfs.py").resolve()
            for item in selected:
                rel = item["path"]
                record = current[rel]
                if record["status"] not in ("SIZE_OK", "STALE"):
                    continue
                verification = run_verification(root, inventory_path, item, args, verifier_path)
                record.setdefault("verification_history", []).append(verification)
                if verification["status"] == "VERIFIED":
                    record["status"] = "VERIFIED"
                elif verification["status"] == "MISMATCH":
                    record["status"] = "MISMATCH"
                else:
                    record["status"] = "STALE" if verification["file_signature"] != record["file_signature"] else "UNKNOWN"
                    record["detail"] = "verification did not complete successfully"

        if args.audit:
            changes = []
            for rel, record in current.items():
                old = previous_entries.get(rel)
                if old is None:
                    changes.append({"path": rel, "change": "new"})
                elif old.get("status") != record.get("status") or old.get("file_signature") != record.get("file_signature"):
                    changes.append({"path": rel, "change": "changed",
                                    "previous_status": old.get("status"),
                                    "current_status": record.get("status")})
            result = {"format": STATE_FORMAT, "inventory_sha256": digest,
                      "audit_at_utc": utc_now(), "changes": changes}
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                if changes:
                    for item in changes:
                        print("{}: {} {}".format(item["path"], item["change"],
                              item.get("previous_status", "") + " -> " + item.get("current_status", "")))
                else:
                    print("No state changes detected.")
            return 1 if changes else 0

        new_entries = dict(previous_entries)
        new_entries.update(current)
        new_state = {"format": STATE_FORMAT, "inventory_sha256": digest,
                     "updated_at_utc": utc_now(), "entries": new_entries}
        atomic_write(state_path, (json.dumps(new_state, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

        displayed = [record for record in current.values()
                     if status_filter is None or record["status"] in status_filter]
        if args.json:
            print(json.dumps({"format": STATE_FORMAT, "inventory_sha256": digest,
                              "updated_at_utc": new_state["updated_at_utc"],
                              "entries": displayed}, indent=2, ensure_ascii=False))
        else:
            for record in displayed:
                detail = " — " + record["detail"] if record.get("detail") else ""
                print("{:<9} {}{}".format(record["status"], record["path"], detail))
            counts = {}
            for record in displayed:
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            print("LFS state: {} entries shown; {}".format(
                len(displayed), ", ".join("{} {}".format(v, k) for k, v in sorted(counts.items()))))
            print("State: {}".format(state_path))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
