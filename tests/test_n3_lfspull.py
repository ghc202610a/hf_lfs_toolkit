import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "lfspull.py"
SPEC = importlib.util.spec_from_file_location("lfspull_module", MODULE_PATH)
lfspull = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lfspull)


class LfsPullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "work"
        self.root.mkdir()
        self.inventory = self.base / "lfs-inventory.json"
        self.log = self.base / "pull.jsonl"
        self.payload = b"abcdef"
        self.entry = {
            "path": "weights/model.bin",
            "oid": "a" * 64,
            "size": len(self.payload),
        }
        (self.root / "weights").mkdir()
        self.write_inventory([self.entry])

    def write_inventory(self, entries):
        self.inventory.write_text(json.dumps({
            "format": "hf-lfs-inventory-v1",
            "repository": "https://huggingface.co/org/model",
            "entries": entries,
        }), encoding="utf-8")

    def run_main(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(lfspull.sys, "argv",
                               ["lfspull.py", "--inventory", str(self.inventory),
                                "--log", str(self.log), "--reserve-gib", "0",
                                *extra]), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = lfspull.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def git_root_result(self):
        return subprocess.CompletedProcess(["git"], 0, str(self.root) + "\n", "")

    def test_retries_failed_pull_then_records_size_correct_completion(self):
        calls = []

        def run(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return self.git_root_result()
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 1, "", "temporary network error")
            (self.root / self.entry["path"]).write_bytes(self.payload)
            return subprocess.CompletedProcess(command, 0, "downloaded\n", "")

        with mock.patch.object(lfspull.subprocess, "run", side_effect=run), \
             mock.patch.object(lfspull.shutil, "disk_usage",
                               return_value=mock.Mock(free=10**12)), \
             mock.patch.object(lfspull.time, "sleep"):
            code, out, err = self.run_main("--retries", "1", "--retry-delay", "0")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(len(calls), 2)
        self.assertIn("SIZE_OK:", out)
        events = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(sum(e["event"] == "attempt-start" for e in events), 2)
        self.assertEqual(events[-1]["event"], "run-finish")
        self.assertEqual(events[-1]["failures"], 0)

    def test_size_correct_file_is_skipped_without_pull(self):
        (self.root / self.entry["path"]).write_bytes(self.payload)

        def run(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return self.git_root_result()
            self.fail("git lfs pull should not be called for a size-correct file")

        with mock.patch.object(lfspull.subprocess, "run", side_effect=run), \
             mock.patch.object(lfspull.shutil, "disk_usage",
                               return_value=mock.Mock(free=10**12)):
            code, out, err = self.run_main()
        self.assertEqual(code, 0, (out, err))
        self.assertIn("SKIP SIZE_OK:", out)

    def test_space_preflight_fails_before_any_pull(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return self.git_root_result()
            self.fail("pull must not start after failed preflight")

        with mock.patch.object(lfspull.subprocess, "run", side_effect=run), \
             mock.patch.object(lfspull.shutil, "disk_usage",
                               return_value=mock.Mock(free=1)):
            code, out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("insufficient free space", err)
        self.assertEqual(len(calls), 1)

    def test_failed_pull_is_retried_and_returns_nonzero(self):
        calls = []

        def run(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return self.git_root_result()
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "offline")

        with mock.patch.object(lfspull.subprocess, "run", side_effect=run), \
             mock.patch.object(lfspull.shutil, "disk_usage",
                               return_value=mock.Mock(free=10**12)), \
             mock.patch.object(lfspull.time, "sleep"):
            code, out, err = self.run_main("--retries", "2", "--retry-delay", "0")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 3)
        self.assertIn("FAILED:", err)

    def test_pointer_is_not_skipped_even_when_its_size_matches_inventory(self):
        expected_size = 0
        for _ in range(5):
            pointer = (
                "version https://git-lfs.github.com/spec/v1\\n"
                "oid sha256:" + "a" * 64 + "\\n"
                "size " + str(expected_size) + "\\n"
            ).encode("ascii")
            expected_size = len(pointer)
        self.assertEqual(len(pointer), expected_size)
        self.entry["size"] = expected_size
        self.write_inventory([self.entry])
        target = self.root / self.entry["path"]
        target.write_bytes(pointer)
        self.assertEqual(
            lfspull.path_status(self.root, self.entry["path"], expected_size),
            ("POINTER", None),
        )

    def test_inventory_rejects_duplicate_and_unsafe_paths(self):
        duplicate = [self.entry, dict(self.entry)]
        self.write_inventory(duplicate)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            lfspull.load_inventory(self.inventory)

        unsafe = dict(self.entry, path="../outside.bin")
        self.write_inventory([unsafe])
        with self.assertRaisesRegex(ValueError, "unsafe"):
            lfspull.load_inventory(self.inventory)

    def test_symlink_path_is_rejected_before_pull(self):
        target = self.base / "outside"
        target.mkdir()
        (self.root / "weights").rmdir()
        (self.root / "weights").symlink_to(target, target_is_directory=True)

        def run(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return self.git_root_result()
            self.fail("symlink path must not be passed to git lfs")

        with mock.patch.object(lfspull.subprocess, "run", side_effect=run), \
             mock.patch.object(lfspull.shutil, "disk_usage",
                               return_value=mock.Mock(free=10**12)):
            code, out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)


if __name__ == "__main__":
    unittest.main()
