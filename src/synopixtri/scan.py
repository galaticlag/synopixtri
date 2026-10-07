"""Inbox scan with a stability delay, so files still being uploaded are never touched."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .models import MEDIA_EXTS, SIDECAR_EXTS, Item, Meta

_TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".!sync", ".temp")


def _is_candidate(name: str) -> bool:
    if name.startswith((".", "~")) or name.lower().endswith(_TEMP_SUFFIXES):
        return False
    return Path(name).suffix.lower() in MEDIA_EXTS | SIDECAR_EXTS


def scan_inbox(
    conn: sqlite3.Connection, inbox: Path, now: float, stability_s: float
) -> list[Item]:
    """List media in the inbox and update the sightings table.

    ``last_change`` is OUR clock at the moment we saw the file with its current size and
    mtime, so stability does not depend on what the uploading app wrote into the mtime.
    A file is stable once it has been unchanged for ``stability_s``.
    """
    known = {
        row["path"]: row
        for row in conn.execute(
            "SELECT path, size, mtime_ns, first_seen, last_change, meta_json, analysis_json FROM inbox_file"
        )
    }
    items: list[Item] = []
    seen: set[str] = set()
    if inbox.is_dir():
        for dirpath, dirnames, filenames in os.walk(inbox):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not _is_candidate(name):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = str(path)
                seen.add(key)
                row = known.get(key)
                meta_json = analysis_json = None
                if row is not None and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns:
                    first_seen, last_change, meta_json = row["first_seen"], row["last_change"], row["meta_json"]
                    analysis_json = row["analysis_json"]
                else:
                    first_seen = row["first_seen"] if row is not None else now
                    last_change = now
                    conn.execute(
                        "INSERT INTO inbox_file(path, size, mtime_ns, first_seen, last_change, meta_json) "
                        "VALUES(?,?,?,?,?,NULL) ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                        "mtime_ns=excluded.mtime_ns, last_change=excluded.last_change, meta_json=NULL, analysis_json=NULL",
                        (key, st.st_size, st.st_mtime_ns, first_seen, last_change),
                    )
                # A file whose mtime is in the future (clock skew) counts as just modified.
                mtime_s = st.st_mtime_ns / 1e9
                newest = now if mtime_s > now else max(last_change, mtime_s)
                item = Item(
                    path=path,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    first_seen=first_seen,
                    last_change=last_change,
                    stable=(now - newest) >= stability_s,
                )
                if meta_json:
                    item.meta = Meta.from_dict(json.loads(meta_json))
                if analysis_json:
                    item.analysis = json.loads(analysis_json)
                items.append(item)
    for gone in set(known) - seen:
        conn.execute("DELETE FROM inbox_file WHERE path=?", (gone,))
    conn.commit()
    return items


def store_meta(conn: sqlite3.Connection, item: Item) -> None:
    if item.meta is not None:
        conn.execute(
            "UPDATE inbox_file SET meta_json=? WHERE path=?",
            (json.dumps(item.meta.to_dict()), str(item.path)),
        )


def store_analysis(conn: sqlite3.Connection, item: Item) -> None:
    if item.analysis is not None:
        conn.execute(
            "UPDATE inbox_file SET analysis_json=? WHERE path=?", (json.dumps(item.analysis), str(item.path))
        )


def forget(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("DELETE FROM inbox_file WHERE path=?", (str(path),))
