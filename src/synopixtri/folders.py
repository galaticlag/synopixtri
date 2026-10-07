"""Index of library folders: rename tracking, adoption of existing folders, lookups."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from . import naming
from .geo import haversine_km


def _walk_inodes(directory: Path) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for dirpath, _dirs, files in os.walk(directory):
        for name in files:
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            found.add((st.st_dev, st.st_ino))
    return found


def _library_dirs(library: Path) -> list[Path]:
    """Folders at depth 2: <library>/<year>/<folder>."""
    dirs: list[Path] = []
    if not library.is_dir():
        return dirs
    for year in sorted(library.iterdir()):
        if year.is_dir() and not year.name.startswith("."):
            dirs += [d for d in sorted(year.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    return dirs


def reconcile(conn: sqlite3.Connection, library: Path) -> int:
    """Detect folders renamed by the user.

    A tracked folder whose path vanished is searched among the unknown library folders by
    file identity (device + inode survive a rename). A folder found under another name now
    belongs to the user: its name is final and we only add to it. Returns the rename count.
    """
    rows = conn.execute("SELECT * FROM folder WHERE missing=0").fetchall()
    vanished = [r for r in rows if not Path(r["path"]).is_dir()]
    if not vanished:
        return 0
    known = {r["path"] for r in rows}
    candidates = {d: None for d in _library_dirs(library) if str(d) not in known}
    renamed = 0
    for row in vanished:
        wanted = {
            (r["dev"], r["ino"])
            for r in conn.execute(
                "SELECT dev, ino FROM placed_file WHERE folder_id=? AND ino IS NOT NULL LIMIT 200",
                (row["id"],),
            )
        }
        match: Path | None = None
        if wanted:
            for cand in list(candidates):
                if candidates[cand] is None:
                    candidates[cand] = _walk_inodes(cand)
                if wanted & candidates[cand]:
                    match = cand
                    break
        if match is None:
            conn.execute("UPDATE folder SET missing=1 WHERE id=?", (row["id"],))
            continue
        del candidates[match]
        status = row["status"]
        if match.name != row["managed_name"]:
            status = "utilisateur"
        conn.execute("UPDATE folder SET path=?, status=? WHERE id=?", (str(match), status, row["id"]))
        renamed += 1
    conn.commit()
    return renamed


def index_library(conn: sqlite3.Connection, library: Path, cfg: SimpleNamespace) -> int:
    """Register library folders we did not create, so new media can be added to them."""
    known = {r["path"] for r in conn.execute("SELECT path FROM folder")}
    added = 0
    for directory in _library_dirs(library):
        if str(directory) in known:
            continue
        parsed = naming.parse_name(directory.name, cfg.routine_label, cfg.event_template, cfg.routine_template)
        if parsed is None:
            continue  # free-form folder: never a target
        conn.execute(
            "INSERT INTO folder(path, role, status, managed_name, date_start, date_end, month_key) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                str(directory), parsed.role, "utilisateur", None,
                parsed.date_start.isoformat(), parsed.date_end.isoformat(), parsed.month_key,
            ),
        )  # fmt: skip
        added += 1
    conn.commit()
    return added


def _near(row: sqlite3.Row, point: tuple[float, float] | None, radius_km: float) -> bool:
    if point is None or row["lat"] is None or row["lon"] is None:
        return True
    return haversine_km((row["lat"], row["lon"]), point) <= radius_km


def find_routine(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM folder WHERE role='routine_mois' AND month_key=? AND missing=0 "
        "ORDER BY id LIMIT 1",
        (key,),
    ).fetchone()


def find_covering(
    conn: sqlite3.Connection, day: date, point: tuple[float, float] | None, radius_km: float
) -> sqlite3.Row | None:
    for row in conn.execute(
        "SELECT * FROM folder WHERE role IN ('evenement','sejour') AND missing=0 "
        "AND date_start<=? AND date_end>=? ORDER BY id",
        (day.isoformat(), day.isoformat()),
    ):
        if _near(row, point, radius_km):
            return row
    return None


def find_adjacent(
    conn: sqlite3.Connection,
    day: date,
    point: tuple[float, float] | None,
    radius_km: float,
    max_gap_days: int,
) -> sqlite3.Row | None:
    """A managed event/stay folder ending (or starting) just before/after ``day``."""
    low = (day - timedelta(days=max_gap_days + 1)).isoformat()
    high = (day + timedelta(days=max_gap_days + 1)).isoformat()
    for row in conn.execute(
        "SELECT * FROM folder WHERE role IN ('evenement','sejour') AND missing=0 AND status!='verrouille' "
        "AND date_end>=? AND date_start<=? ORDER BY id",
        (low, high),
    ):
        if _near(row, point, radius_km):
            return row
    return None


def placed_units_on(conn: sqlite3.Connection, day: date) -> dict[str, int]:
    """Units we already placed for that capture date, by folder role."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT f.role AS role, COUNT(DISTINCT p.unit_key) AS n FROM placed_file p "
        "JOIN folder f ON f.id=p.folder_id WHERE p.ref_date=? GROUP BY f.role",
        (day.isoformat(),),
    ):
        counts[row["role"]] = row["n"]
    return counts


def placed_routine_between(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, f.path AS folder_path FROM placed_file p JOIN folder f ON f.id=p.folder_id "
        "WHERE f.role='routine_mois' AND p.ref_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


def to_name(conn: sqlite3.Connection, placeholder: str, event_template: str = naming.EVENT_DEFAULT) -> list[dict]:
    """Event and stay folders still carrying the provisional label."""
    result = []
    for row in conn.execute(
        "SELECT * FROM folder WHERE role IN ('evenement','sejour') AND missing=0 "
        "AND status!='utilisateur' ORDER BY date_start"
    ):
        parsed = naming.parse_name(Path(row["path"]).name, "", event_template)
        if parsed and parsed.label == placeholder:
            result.append({"path": row["path"], "date_start": row["date_start"], "date_end": row["date_end"]})
    return result
