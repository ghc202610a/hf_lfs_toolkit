import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LFS_VERSION = "https://git-lfs.github.com/spec/v1"
OID = hashlib.sha256(b"data").hexdigest()


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class N1RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load("snapshot_n1", "hf-lfs-snapshot.py")
        cls.verify = load("verify_n1", "verify-lfs.py")

    @staticmethod
    def lfs_item(name="weights/model.bin", oid=OID, size=4):
        return {
            "name": name,
            "oid_type": "sha256",
            "oid": oid,
            "size": size,
            "version": LFS_VERSION,
        }

    def test_snapshot_parser_accepts_valid_entry_and_preserves_optional_fields(self):
        item = self.lfs_item()
        item.update(checkout=True, downloaded=False)
        payload, entries = self.snapshot.parse_lfs_json(
            json.dumps({"files": [item]}).encode()
        )
        self.assertEqual(payload["files"], [item])
        self.assertEqual(entries[0]["path"], item["name"])
        self.assertTrue(entries[0]["checkout"])
        self.assertFalse(entries[0]["downloaded"])

    def test_snapshot_parser_rejects_malformed_json_and_bad_container(self):
        for raw in (b"not json", b"[]", b'{"files": {}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.snapshot.parse_lfs_json(raw)

    def test_snapshot_parser_rejects_duplicate_paths(self):
        item = self.lfs_item()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.snapshot.parse_lfs_json(
                json.dumps({"files": [item, item]}).encode()
            )

    def test_snapshot_parser_rejects_unsafe_paths_and_invalid_metadata(self):
        bad_items = [
            self.lfs_item(name="../escape"),
            self.lfs_item(name="/absolute"),
            self.lfs_item(oid="A" * 64),
            self.lfs_item(size=True),
            self.lfs_item(size=-1),
            self.lfs_item(size="4"),
        ]
        for item in bad_items:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.snapshot.parse_lfs_json(
                    json.dumps({"files": [item]}).encode()
                )

    def test_generated_scripts_quote_repository_and_lfs_paths(self):
        odd_path = "dir/it's a file; $(not-a-command).bin"
        raw = json.dumps({"files": [self.lfs_item(name=odd_path)]}).encode()
        listing = b"listing\n"
        commit = "0123456789abcdef"

        def fake_run(cmd, *, cwd=None, env=None, capture=False):
            if cmd[:2] == ["git", "clone"]:
                Path(cmd[-1]).mkdir()
                return None
            if cmd[1:4] == ["lfs", "ls-files", "--json"]:
                return raw
            if cmd[1:4] == ["lfs", "ls-files", "--long"]:
                return listing
            if cmd[1:3] == ["rev-parse", "HEAD"]:
                return (commit + "\n").encode()
            raise AssertionError(f"unexpected command: {cmd!r}")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            with mock.patch.object(self.snapshot, "run", side_effect=fake_run):
                with mock.patch.object(sys, "argv", [
                    "hf-lfs-snapshot.py", "https://huggingface.co/org/model",
                    "--out", td, "--name", "capture", "--no-archive",
                ]):
                    self.snapshot.main()

            pull = (out / "capture" / "local.lfspull.sh").read_text()
            clone = (out / "capture" / "_clone.sh").read_text()
            self.assertIn("git lfs pull --include=" + __import__("shlex").quote(odd_path), pull)
            self.assertIn("COMMIT='0123456789abcdef'", clone)
            self.assertTrue(os.access(out / "capture" / "_clone.sh", os.X_OK))
            self.assertTrue(os.access(out / "capture" / "local.lfspull.sh", os.X_OK))
            self.assertFalse((out / "capture.tar.gz").exists())

    def test_snapshot_creates_archive_by_default_with_expected_members(self):
        raw = json.dumps({"files": [self.lfs_item()]}).encode()

        def fake_run(cmd, *, cwd=None, env=None, capture=False):
            if cmd[:2] == ["git", "clone"]:
                Path(cmd[-1]).mkdir()
                return None
            if cmd[1:4] == ["lfs", "ls-files", "--json"]:
                return raw
            if cmd[1:4] == ["lfs", "ls-files", "--long"]:
                return b"listing\n"
            if cmd[1:3] == ["rev-parse", "HEAD"]:
                return b"deadbeef\n"
            raise AssertionError(f"unexpected command: {cmd!r}")

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(self.snapshot, "run", side_effect=fake_run):
                with mock.patch.object(sys, "argv", [
                    "hf-lfs-snapshot.py", "https://huggingface.co/org/model",
                    "--out", td, "--name", "capture",
                ]):
                    self.snapshot.main()
            archive = Path(td) / "capture.tar.gz"
            self.assertTrue(archive.is_file())
            with tarfile.open(archive, "r:gz") as tf:
                names = set(tf.getnames())
            for name in (
                "capture/local.lfsfiles.json",
                "capture/local.lfsfiles.txt",
                "capture/lfs-inventory.json",
                "capture/_clone.sh",
                "capture/local.lfspull.sh",
            ):
                self.assertIn(name, names)

    def test_inventory_rejects_duplicate_paths_invalid_oid_and_size(self):
        cases = [
            [{"path": "x", "oid": OID, "size": 1},
             {"path": "x", "oid": OID, "size": 1}],
            [{"path": "x", "oid": "z" * 64, "size": 1}],
            [{"path": "x", "oid": OID, "size": True}],
            [{"path": "../x", "oid": OID, "size": 1}],
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "inventory.json"
            for entries in cases:
                path.write_text(json.dumps({
                    "format": "hf-lfs-inventory-v1", "entries": entries,
                }))
                with self.subTest(entries=entries), self.assertRaises(ValueError):
                    self.verify.load_inventory(path)

    def test_checksum_manifest_escapes_backslash_and_newline(self):
        line = self.verify.checksum_line(OID, "a\\b\nc")
        self.assertEqual(line, b"\\" + OID.encode() + b"  a\\\\b\\nc\n")

    def test_symlink_component_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "real").mkdir()
            (root / "real" / "f").write_text("data")
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.verify.reject_symlink_path(root, root / "link" / "f")

    def test_symlink_leaf_and_non_regular_files_are_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.write_text("data")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.verify.reject_symlink_path(root, link)
            self.assertEqual(
                self.verify.file_signature(root),
                ("not-regular", root.stat().st_mode),
            )

    def test_hashcheck_resolution_reports_missing_dependency(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            self.verify.shutil, "which", return_value=None
        ):
            with self.assertRaises(FileNotFoundError):
                self.verify.resolve_hashcheck(None)

    def _run_verifier(self, temp_root, hash_status):
        root = temp_root / "worktree"
        (root / ".git" / "lfs" / "objects" / OID[:2] / OID[2:4]).mkdir(parents=True)
        (root / ".git" / "lfs" / "incomplete").mkdir(parents=True)
        content = b"data"
        (root / "weights.bin").write_bytes(content)
        cache_object = root / ".git" / "lfs" / "objects" / OID[:2] / OID[2:4] / OID
        cache_object.write_bytes(content)
        incomplete = root / ".git" / "lfs" / "incomplete" / "keep.tmp"
        incomplete.write_bytes(b"keep")
        inventory = temp_root / "inventory.json"
        inventory.write_text(json.dumps({
            "format": "hf-lfs-inventory-v1",
            "entries": [{"path": "weights.bin", "oid": OID, "size": len(content)}],
        }))
        hashcheck = temp_root / "hashcheck.py"
        hashcheck.write_text("# mock executable placeholder\n")

        def fake_subprocess_run(cmd, **kwargs):
            if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(cmd, 0, str(root) + "\n", "")
            if cmd[:3] == ["git", "rev-parse", "--git-dir"]:
                return subprocess.CompletedProcess(cmd, 0, ".git\n", "")
            if len(cmd) > 1 and cmd[1] == str(hashcheck):
                return subprocess.CompletedProcess(
                    cmd, hash_status, b"hash output", b"hash warning"
                )
            raise AssertionError(f"unexpected subprocess: {cmd!r}")

        argv = [
            "verify-lfs.py", "--inventory", str(inventory),
            "--hashcheck", str(hashcheck), "--run-id", "test-run",
            "--prune", "--manifest-out", str(temp_root / "expected.txt"),
            "--sha256-out", str(temp_root / "computed256.txt"),
            "--sha512-out", str(temp_root / "computed512.txt"),
            "--log-out", str(temp_root / "hash.log"),
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            self.verify.subprocess, "run", side_effect=fake_subprocess_run
        ):
            result = self.verify.main()
        return result, cache_object, incomplete

    def test_verifier_success_prunes_only_verified_object_and_preserves_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            result, cache_object, incomplete = self._run_verifier(Path(td), 0)
            self.assertEqual(result, 0)
            self.assertFalse(cache_object.exists())
            self.assertTrue(incomplete.exists())

    def test_verifier_checksum_failure_does_not_prune_anything(self):
        with tempfile.TemporaryDirectory() as td:
            result, cache_object, incomplete = self._run_verifier(Path(td), 1)
            self.assertEqual(result, 1)
            self.assertTrue(cache_object.exists())
            self.assertTrue(incomplete.exists())


if __name__ == "__main__":
    unittest.main()
