"""Exact duplicate detection. Files are only hashed when another file has the same size."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path

from .models import Unit


def file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - content fingerprint, not security
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def find_duplicates(conn: sqlite3.Connection, units: list[Unit], library_root: Path) -> dict[str, str]:
    """Map unit key -> reason for every unit whose primary file duplicates another file.

    A unit is a duplicate if an identical file is already in the library (placed by us) or
    if it repeats an earlier unit of the same batch. The first copy of a batch is kept.
    """
    result: dict[str, str] = {}
    by_size: dict[int, list[Unit]] = defaultdict(list)
    for unit in units:
        by_size[unit.primary.size].append(unit)

    for size, group in by_size.items():
        placed = conn.execute(
            "SELECT p.id, p.md5, p.rel_path, f.path AS folder_path FROM placed_file p "
            "JOIN folder f ON f.id=p.folder_id WHERE p.size=? AND p.is_live=0",
            (size,),
        ).fetchall()
        if len(group) < 2 and not placed:
            continue
        library_hashes: set[str] = set()
        for row in placed:
            digest = row["md5"]
            if digest is None:
                path = Path(row["folder_path"]) / row["rel_path"]
                if not path.is_file():
                    continue
                digest = file_md5(path)
                conn.execute("UPDATE placed_file SET md5=? WHERE id=?", (digest, row["id"]))
            library_hashes.add(digest)
        seen: set[str] = set()
        for unit in sorted(group, key=lambda u: str(u.primary.path)):
            digest = file_md5(unit.primary.path)
            if digest in library_hashes:
                result[unit.key] = "duplicate_of_library"
            elif digest in seen:
                result[unit.key] = "duplicate_in_batch"
            seen.add(digest)
    conn.commit()
    return result
