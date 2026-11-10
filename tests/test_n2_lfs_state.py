import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OID = "a" * 64


def load_module():
    spec = importlib.util.spec_from_file_location("lfsls_n2", ROOT / "lfsls.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LFSStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def entry(self, path="weights.bin", size=10, oid=OID):
        return {"path": path, "oid": oid, "size": size}

    def test_pointer_and_materialized_size_classification(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "weights.bin"
            pointer = (
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:{}\nsize 10\n".format(OID)
            )
            path.write_text(pointer)
            result = self.mod.observe(root, self.entry(), {}, "inventory-hash")
            self.assertEqual(result["status"], "POINTER")

            path.write_bytes(b"12345")
            result = self.mod.observe(root, self.entry(), {}, "inventory-hash")
            self.assertEqual(result["status"], "PARTIAL")

            path.write_bytes(b"1234567890")
            result = self.mod.observe(root, self.entry(), {}, "inventory-hash")
            self.assertEqual(result["status"], "SIZE_OK")

            path.write_bytes(b"12345678901")
            result = self.mod.observe(root, self.entry(), {}, "inventory-hash")
            self.assertEqual(result["status"], "MISMATCH")

    def test_matching_verification_is_reused_and_changed_file_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "weights.bin"
            path.write_bytes(b"0123456789")
            sig, status, error = self.mod.signature(path)
            verification = {
                "status": "VERIFIED",
                "inventory_sha256": "inventory-hash",
                "file_signature": sig,
            }
            previous = {"verification_history": [verification]}
            result = self.mod.observe(root, self.entry(), previous, "inventory-hash")
            self.assertEqual(result["status"], "VERIFIED")

            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000000))
            result = self.mod.observe(root, self.entry(), previous, "inventory-hash")
            self.assertEqual(result["status"], "STALE")

    def test_symlinked_intermediate_component_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "worktree"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "weights.bin").write_bytes(b"0123456789")
            (root / "linked").symlink_to(outside, target_is_directory=True)
            result = self.mod.observe(
                root, self.entry(path="linked/weights.bin"), {}, "inventory-hash"
            )
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertIn("symlink", result["detail"])

    def test_missing_file_and_non_regular_file_states(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.mod.observe(root, self.entry(), {}, "inventory-hash")
            self.assertEqual(result["status"], "MISSING")

            path = root / "weights.bin"
            path.mkdir()
            result = self.mod.observe(root, self.entry(), {}, "inventory-hash")
            self.assertEqual(result["status"], "UNKNOWN")

    def test_inventory_change_invalidates_cached_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "local.lfsstate.json"
            path.write_text(json.dumps({
                "format": "hf-lfs-state-v1",
                "inventory_sha256": "old-hash",
                "entries": {"weights.bin": {"status": "VERIFIED"}},
            }))
            state = self.mod.read_state(path, "new-hash")
            self.assertEqual(state["entries"], {})
            self.assertEqual(state["inventory_sha256"], "new-hash")

    def test_inventory_rejects_duplicate_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "inventory.json"
            entry = self.entry()
            path.write_text(json.dumps({
                "format": "hf-lfs-inventory-v1",
                "entries": [entry, entry],
            }))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.mod.load_inventory(path)

            path.write_text(json.dumps({
                "format": "hf-lfs-inventory-v1",
                "entries": [self.entry(path="../escape")],
            }))
            with self.assertRaisesRegex(ValueError, "unsafe"):
                self.mod.load_inventory(path)


if __name__ == "__main__":
    unittest.main()
