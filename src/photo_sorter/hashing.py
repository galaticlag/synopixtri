from __future__ import annotations

import hashlib
from pathlib import Path


def file_hash(path: Path, *, algorithm: str = "md5", chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
