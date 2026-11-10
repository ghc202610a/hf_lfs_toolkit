import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class N1RegressionTests(unittest.TestCase):
    def test_snapshot_parser_rejects_duplicate_paths(self):
        mod = load("snapshot_n1", "hf-lfs-snapshot.py")
        item = {
            "name": "x.bin",
            "oid_type": "sha256",
            "oid": "a" * 64,
            "size": 1,
            "version": "https://git-lfs.github.com/spec/v1",
        }
        with self.assertRaises(ValueError):
            mod.parse_lfs_json(json.dumps({"files": [item, item]}).encode())

    def test_symlink_component_rejected(self):
        mod = load("verify_n1", "verify-lfs.py")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "real").mkdir()
            (root / "real" / "f").write_text("x")
            (root / "link").symlink_to(
                root / "real", target_is_directory=True
            )
            with self.assertRaises(ValueError):
                mod.reject_symlink_path(root, root / "link" / "f")

if __name__ == "__main__":
    unittest.main()
