#!/usr/bin/env python3
"""Small streaming multi-digest core used by the HF LFS verifier.

The API is intentionally simple: hash_file(filename, algorithms=..., block_size=...)
returns {algorithm: lowercase_hex_digest}. All requested digests are updated
from the same read pass.
"""
from __future__ import annotations
import hashlib
from pathlib import Path

DEFAULT_BLOCK_SIZE = 8 * 1024 * 1024


def hash_file(filename, algorithms=("sha256", "sha512"), block_size=DEFAULT_BLOCK_SIZE):
    names = tuple(dict.fromkeys(str(a).lower().replace("-", "") for a in algorithms))
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    hashers = {}
    for name in names:
        try:
            hashers[name] = hashlib.new(name)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"unsupported hash algorithm: {name}") from exc
    with open(Path(filename), "rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            for hasher in hashers.values():
                hasher.update(block)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}
