import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "lfsbackends.py"
SPEC = importlib.util.spec_from_file_location("lfsbackends_module", MODULE_PATH)
backends = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backends
SPEC.loader.exec_module(backends)


class HubBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "work"
        (self.root / "weights").mkdir(parents=True)
        self.cache = self.base / "hub-cache"
        self.cache.mkdir()
        self.cached_file = self.base / "cached-payload.bin"
        self.payload = b"hub-cache-payload"
        self.cached_file.write_bytes(self.payload)
        self.entry = {
            "path": "weights/model.bin",
            "oid": "a" * 64,
            "size": len(self.payload),
        }
        self.context = backends.AcquisitionContext(
            root=self.root,
            repository="https://huggingface.co/org/model",
            git_commit="b" * 40,
            cache_dir=self.cache,
        )

    def install_fake_hub(self, callback):
        fake = types.ModuleType("huggingface_hub")
        fake.hf_hub_download = callback
        return mock.patch.dict(sys.modules, {"huggingface_hub": fake})

    def test_hub_download_uses_exact_commit_and_preserves_cache_file(self):
        calls = []

        def download(**kwargs):
            calls.append(kwargs)
            return str(self.cached_file)

        with self.install_fake_hub(download):
            result = backends.HuggingFaceHubBackend().acquire(self.entry, self.context)

        self.assertEqual(result.outcome, "SIZE_OK")
        self.assertEqual(result.observed_bytes, len(self.payload))
        self.assertEqual(calls, [{
            "repo_id": "org/model",
            "filename": "weights/model.bin",
            "revision": "b" * 40,
            "repo_type": "model",
            "cache_dir": str(self.cache),
        }])
        self.assertEqual((self.root / self.entry["path"]).read_bytes(), self.payload)
        self.assertEqual(self.cached_file.read_bytes(), self.payload)
        self.assertFalse(list((self.root / "weights").glob(".model.bin.hfhub-*")))

    def test_replacement_preserves_existing_file_mode(self):
        target = self.root / self.entry["path"]
        target.write_bytes(b"old")
        target.chmod(0o640)

        with self.install_fake_hub(lambda **kwargs: str(self.cached_file)):
            result = backends.HuggingFaceHubBackend().acquire(self.entry, self.context)

        self.assertEqual(result.outcome, "SIZE_OK")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_size_mismatch_does_not_install_or_replace_target(self):
        target = self.root / self.entry["path"]
        target.write_bytes(b"old")

        def download(**kwargs):
            return str(self.cached_file)

        self.entry["size"] += 1
        with self.install_fake_hub(download):
            result = backends.HuggingFaceHubBackend().acquire(self.entry, self.context)

        self.assertEqual(result.outcome, "ERROR")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(result.detail, "Hub cache file size differs from captured inventory")

    def test_repository_url_accepts_canonical_model_urls(self):
        for url in (
            "https://huggingface.co/org/model",
            "https://huggingface.co/org/model/",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    backends.HuggingFaceHubBackend.repository_id(url),
                    "org/model",
                )

    def test_repository_url_rejects_noncanonical_or_ambiguous_urls(self):
        invalid_urls = (
            "http://huggingface.co/org/model",
            "ftp://huggingface.co/org/model",
            "https://user@huggingface.co/org/model",
            "https://user:secret@huggingface.co/org/model",
            "https://huggingface.co:443/org/model",
            "https://huggingface.co:bad/org/model",
            "https://huggingface.co/org/model?download=1",
            "https://huggingface.co/org/model?",
            "https://huggingface.co/org/model#fragment",
            "https://huggingface.co/org/model#",
            "https://huggingface.co/datasets/org/data",
            "https://huggingface.co/org/model/extra",
            "https://huggingface.co//org/model",
            "https://huggingface.co/org%2Fother/model",
            "https://huggingface.co/org/model;params",
            "https://www.huggingface.co/org/model",
            "https://example.com/org/model",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    backends.HuggingFaceHubBackend.repository_id(url)

    def test_repository_url_scope_is_model_only(self):
        with self.assertRaisesRegex(ValueError, "model repository"):
            backends.HuggingFaceHubBackend.repository_id("https://huggingface.co/datasets/org/data")

    def test_exception_text_is_not_exposed_in_diagnostic(self):
        def download(**kwargs):
            raise RuntimeError("https://signed.example/download?token=secret")

        with self.install_fake_hub(download):
            result = backends.HuggingFaceHubBackend().acquire(self.entry, self.context)

        self.assertEqual(result.outcome, "ERROR")
        self.assertNotIn("secret", result.detail)
        self.assertNotIn("signed.example", result.detail)

    def test_invalid_commit_is_rejected_before_hub_call(self):
        self.context.git_commit = "main"
        with self.install_fake_hub(lambda **kwargs: self.fail("Hub must not be called")):
            result = backends.HuggingFaceHubBackend().acquire(self.entry, self.context)
        self.assertEqual(result.outcome, "ERROR")
        self.assertIn("full commit ID", result.detail)


if __name__ == "__main__":
    unittest.main()
