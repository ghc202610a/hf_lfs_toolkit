import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "lfscache.py"
SPEC = importlib.util.spec_from_file_location("lfscache_module", MODULE_PATH)
lfscache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lfscache)


class LfsCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cache = self.base / "objects"
        self.cache.mkdir()
        self.objects = self.cache / "objects"
        self.objects.mkdir()
        self.oid = "a" * 64
        self.object_path = self.objects / self.oid[:2] / self.oid[2:4] / self.oid[4:]
        self.object_path.parent.mkdir(parents=True)
        self.object_path.write_bytes(b"payload")
        self.inventory = self.base / "inventory.json"
        self.inventory.write_text(json.dumps({
            "format": "hf-lfs-inventory-v1",
            "entries": [{"path": "model.bin", "oid": self.oid, "size": 7}],
        }), encoding="utf-8")

    def test_inventory_validation_and_oid_deduplication(self):
        result = lfscache.load_inventory(self.inventory)
        self.assertEqual(set(result), {self.oid})
        self.assertEqual(result[self.oid]["expected_size"], 7)

    def test_scan_finds_only_canonical_object_layout(self):
        objects, links, unreadable = lfscache.scan_cache(self.cache)
        self.assertEqual([item["oid"] for item in objects], [self.oid])
        self.assertEqual((links, unreadable), (0, 0))
        junk = self.objects / "not-an-object"
        junk.write_text("ignored")
        objects, _, _ = lfscache.scan_cache(self.cache)
        self.assertEqual(len(objects), 1)

    def test_symlink_is_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / ("b" * 64)).write_bytes(b"external")
        (self.objects / "link").symlink_to(outside, target_is_directory=True)
        objects, links, _ = lfscache.scan_cache(self.cache)
        self.assertEqual(len(objects), 1)
        self.assertEqual(links, 1)

    def test_object_oid_requires_canonical_two_two_sixty_layout(self):
        self.assertEqual(
            lfscache.object_oid(Path(self.oid[:2]) / self.oid[2:4] / self.oid[4:]),
            self.oid,
        )
        self.assertIsNone(lfscache.object_oid(Path(self.oid)))
        self.assertIsNone(lfscache.object_oid(Path("zz") / self.oid[2:4] / self.oid[4:]))

    def test_conflicting_inventory_sizes_rejected(self):
        self.inventory.write_text(json.dumps({
            "format": "hf-lfs-inventory-v1",
            "entries": [
                {"path": "one", "oid": self.oid, "size": 7},
                {"path": "two", "oid": self.oid, "size": 8},
            ],
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "conflicting sizes"):
            lfscache.load_inventory(self.inventory)


if __name__ == "__main__":
    unittest.main()
