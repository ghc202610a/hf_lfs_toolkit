import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("lfspull_n5_module", ROOT / "lfspull.py")
lfspull = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lfspull)


class HubRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "work"
        (self.root / "weights").mkdir(parents=True)
        self.inventory = self.base / "inventory.json"
        self.log = self.base / "pull.jsonl"
        self.cache_file = self.base / "cached.bin"
        self.payload = b"hub-materialized-data"
        self.cache_file.write_bytes(self.payload)
        self.entry = {"path": "weights/model.bin", "oid": "a" * 64, "size": len(self.payload)}
        self.inventory.write_text(json.dumps({
            "format": "hf-lfs-inventory-v1",
            "repository": "https://huggingface.co/org/model",
            "git_commit": "b" * 40,
            "entries": [self.entry],
        }), encoding="utf-8")

    def run_main(self, *extra):
        fake = types.ModuleType("huggingface_hub")
        fake.hf_hub_download = lambda **kwargs: str(self.cache_file)
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["lfspull.py", "--inventory", str(self.inventory),
                "--log", str(self.log), "--reserve-gib", "0",
                "--backend", "hf-hub", "--retries", "0", *extra]
        root_result = subprocess.CompletedProcess(["git"], 0, str(self.root) + "\n", "")
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake}), \\
             mock.patch.object(lfspull.sys, "argv", argv), \\
             mock.patch.object(lfspull.subprocess, "run", return_value=root_result), \\
             mock.patch.object(lfspull.shutil, "disk_usage", return_value=mock.Mock(free=10**12)), \\
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = lfspull.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_hub_backend_materializes_and_logs_backend(self):
        code, out, err = self.run_main()
        self.assertEqual(code, 0, (out, err))
        self.assertEqual((self.root / self.entry["path"]).read_bytes(), self.payload)
        events = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(events[0]["backend"], "hf-hub")
        self.assertEqual(events[-1]["failures"], 0)

    def test_partial_materialized_target_requires_replace(self):
        target = self.root / self.entry["path"]
        target.write_bytes(b"partial")
        code, out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("use --replace", err)
        self.assertEqual(target.read_bytes(), b"partial")

    def test_replace_authorizes_wrong_size_regular_file(self):
        target = self.root / self.entry["path"]
        target.write_bytes(b"old-partial")
        code, out, err = self.run_main("--replace")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(target.read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()
