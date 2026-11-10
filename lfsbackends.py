#!/usr/bin/env python3
"""Acquisition backend adapters for inventory-driven Hugging Face LFS pulls.

The Hub adapter delegates authentication, HTTP/Xet transport, retries, and cache
management to huggingface_hub. This module does not hash file contents.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class AcquisitionResult:
    backend: str
    outcome: str
    observed_bytes: int | None = None
    detail: str | None = None


@dataclass
class AcquisitionContext:
    root: Path
    repository: str
    git_commit: str
    cache_dir: Path | None = None
    disable_xet: bool = False


class AcquisitionBackend:
    name = "abstract"

    def acquire(self, entry: dict, context: AcquisitionContext) -> AcquisitionResult:
        raise NotImplementedError


class GitLFSBackend(AcquisitionBackend):
    """Marker adapter; the existing runner owns Git LFS subprocess handling."""
    name = "git-lfs"


class HuggingFaceHubBackend(AcquisitionBackend):
    name = "hf-hub"

    @staticmethod
    def repository_id(repository: str) -> str:
        parsed = urlparse(repository)
        if parsed.scheme not in ("https", "http") or parsed.hostname not in (
            "huggingface.co", "www.huggingface.co"
        ):
            raise ValueError("Hub backend requires a huggingface.co model repository URL")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or any(part in (".", "..") for part in parts):
            raise ValueError("Hub backend supports model repository URLs in ORG/MODEL form")
        return "/".join(parts)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Return a compact category, never a URL, token, or raw server response."""
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        if "gated" in name or "gated" in message:
            return "gated repository access denied"
        if "repositorynotfound" in name or "revisionnotfound" in name:
            return "repository or captured revision not found (or not accessible)"
        if "unauthorized" in name or "authentication" in name or "token" in name:
            return "authentication or authorization failed"
        if "ratelimit" in name or "429" in message:
            return "Hub rate limit encountered"
        if "timeout" in name or "connection" in name or "http" in name:
            return "Hub network request failed ({})".format(type(exc).__name__)
        return "Hub acquisition failed ({})".format(type(exc).__name__)

    def acquire(self, entry: dict, context: AcquisitionContext) -> AcquisitionResult:
        revision = context.git_commit
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            return AcquisitionResult(self.name, "ERROR", detail="inventory git_commit is not a full commit ID")

        try:
            repo_id = self.repository_id(context.repository)
        except ValueError as exc:
            return AcquisitionResult(self.name, "ERROR", detail=str(exc))

        if context.disable_xet:
            # Must be set before importing huggingface_hub's download implementation.
            os.environ["HF_HUB_DISABLE_XET"] = "1"

        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            return AcquisitionResult(
                self.name, "ERROR",
                detail="optional dependency missing; install a compatible huggingface_hub package",
            )

        kwargs = {
            "repo_id": repo_id,
            "filename": entry["path"],
            "revision": revision,
            "repo_type": "model",
        }
        if context.cache_dir is not None:
            kwargs["cache_dir"] = str(context.cache_dir)

        try:
            cached_name = hf_hub_download(**kwargs)
            cached = Path(cached_name)
            # Hub cache entries can intentionally be symlinks to shared blobs.
            if not cached.is_file():
                return AcquisitionResult(self.name, "ERROR", detail="Hub cache result is not a readable file")
            observed = cached.stat().st_size
            if observed != entry["size"]:
                return AcquisitionResult(
                    self.name, "ERROR", observed,
                    "Hub cache file size differs from captured inventory",
                )

            target = context.root.joinpath(*Path(entry["path"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
            fd, temp_name = tempfile.mkstemp(prefix="." + target.name + ".hfhub-", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as output, cached.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                staged = Path(temp_name)
                if staged.stat().st_size != entry["size"]:
                    raise OSError("staged file size changed during cache copy")
                os.chmod(staged, existing_mode)
                os.replace(staged, target)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise

            return AcquisitionResult(self.name, "SIZE_OK", observed)
        except Exception as exc:
            return AcquisitionResult(self.name, "ERROR", detail=self._safe_error(exc))


def hub_diagnostics(cache_dir: Path | None = None) -> dict:
    """Return non-secret cache/transport diagnostics without importing the Hub."""
    try:
        xet_available = importlib.util.find_spec("hf_xet") is not None
    except (ImportError, ValueError):
        xet_available = False
    disabled = os.environ.get("HF_HUB_DISABLE_XET", "").strip().lower() in ("1", "true", "yes")
    try:
        from huggingface_hub.constants import HF_HUB_CACHE, HF_XET_CACHE
        hub_cache = str(cache_dir or HF_HUB_CACHE)
        xet_cache = str(HF_XET_CACHE)
    except ImportError:
        hub_cache = str(cache_dir) if cache_dir else None
        xet_cache = os.environ.get("HF_XET_CACHE")
    return {
        "hub_cache": hub_cache,
        "xet_cache": xet_cache,
        "hf_xet_installed": xet_available,
        "xet_disabled": disabled,
        "xet_may_be_used": xet_available and not disabled,
    }
