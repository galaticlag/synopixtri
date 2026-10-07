"""Housekeeping around a pass: proposals, set-aside folder (restore, optional purge), exemptions."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from .config import Paths
from .executor import move_file, unique_path

ASIDE_REASONS = ("aside_rule", "aside_zone")


def save_proposals(conn: sqlite3.Connection, proposals: list, now: float) -> None:
    conn.execute("DELETE FROM proposal")
    for p in proposals:
        conn.execute(
            "INSERT INTO proposal(start_date, end_date, lat, lon, place, count, paths_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                p.start.isoformat(), p.end.isoformat(), p.point[0] if p.point else None,
                p.point[1] if p.point else None, p.place, p.count, json.dumps(p.paths), now,
            ),
        )  # fmt: skip
    conn.commit()


def clean_exempt(conn: sqlite3.Connection) -> None:
    """A restored file is exempt until it leaves the inbox."""
    for row in conn.execute("SELECT path FROM exempt").fetchall():
        if not Path(row["path"]).exists():
            conn.execute("DELETE FROM exempt WHERE path=?", (row["path"],))
    conn.commit()


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved != root.resolve() and root.resolve() in resolved.parents


def list_aside(conn: sqlite3.Connection, paths: Paths, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        "SELECT o.id, o.src, o.dst, o.why, o.reason, j.started_at FROM operation_log o "
        "JOIN job j ON j.id=o.job_id WHERE o.op='move' AND o.state='done' "
        "AND o.reason IN ('aside_rule','aside_zone') ORDER BY o.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        dst = Path(r["dst"])
        if not dst.is_file() or not _inside(dst, paths.aside):
            continue
        result.append(
            {
                "id": r["id"], "name": dst.name, "path": str(dst), "folder": dst.parent.name,
                "size": dst.stat().st_size, "why": r["why"], "moved_at": r["started_at"], "origin": r["src"],
            }
        )  # fmt: skip
    return result


def restore_aside(conn: sqlite3.Connection, paths: Paths, op_ids: list[int], now: float | None = None) -> dict:
    """Bring files back to the inbox. They are exempt from rules and zones until they are filed."""
    now = time.time() if now is None else now
    cur = conn.execute("INSERT INTO job(kind, state, dry_run, started_at) VALUES('restore','running',0,?)", (now,))
    job_id = int(cur.lastrowid)
    restored, failed, seq = 0, [], 0
    for op_id in op_ids:
        row = conn.execute(
            "SELECT * FROM operation_log WHERE id=? AND op='move' AND state='done' "
            "AND reason IN ('aside_rule','aside_zone')", (op_id,),
        ).fetchone()  # fmt: skip
        if row is None or not row["dst"] or not row["src"]:
            failed.append({"id": op_id, "error": "not a set-aside file"})
            continue
        dst, src = Path(row["dst"]), Path(row["src"])
        if not dst.is_file() or not _inside(dst, paths.aside) or not _inside(src, paths.inbox):
            failed.append({"id": op_id, "error": "file missing or outside the expected folders"})
            continue
        target = unique_path(src)
        try:
            move_file(dst, target)
        except OSError as exc:
            failed.append({"id": op_id, "error": str(exc)})
            continue
        seq += 1
        conn.execute(
            "INSERT INTO operation_log(job_id, seq, op, src, dst, state, reason, why) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, seq, "move", str(dst), str(target), "done", "restore", "Remis dans l'arrivée à ta demande"),
        )
        conn.execute("INSERT OR IGNORE INTO exempt(path) VALUES(?)", (str(target),))
        conn.execute("UPDATE operation_log SET state='restored' WHERE id=?", (op_id,))
        restored += 1
    conn.execute(
        "UPDATE job SET state='done', finished_at=?, stats_json=? WHERE id=?",
        (time.time(), json.dumps({"restored": restored, "failed": len(failed)}), job_id),
    )
    conn.commit()
    return {"job_id": job_id, "restored": restored, "failed": failed}


def purge_aside(
    conn: sqlite3.Connection, job_id: int, cfg: SimpleNamespace, paths: Paths, now: float, dry_run: bool
) -> int:
    """Delete set-aside files older than ``aside_purge_days``. Opt-in: needs the confirmation flag."""
    days = int(cfg.aside_purge_days)
    if days <= 0 or not cfg.aside_purge_confirmed:
        return 0
    rows = conn.execute(
        "SELECT o.id, o.dst FROM operation_log o JOIN job j ON j.id=o.job_id WHERE o.op='move' "
        "AND o.state='done' AND o.reason IN ('aside_rule','aside_zone') AND j.started_at < ?",
        (now - days * 86400,),
    ).fetchall()
    count = 0
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM operation_log WHERE job_id=?", (job_id,)).fetchone()[0]
    for row in rows:
        path = Path(row["dst"])
        if not path.is_file() or not _inside(path, paths.aside):
            continue
        count += 1
        if dry_run:
            continue
        try:
            path.unlink()
        except OSError:
            count -= 1
            continue
        seq += 1
        conn.execute("UPDATE operation_log SET state='purged' WHERE id=?", (row["id"],))
        conn.execute(
            "INSERT INTO operation_log(job_id, seq, op, src, state, reason, why) VALUES(?,?,?,?,?,?,?)",
            (job_id, seq, "purge", str(path), "done", "aside_purge", f"Mis de côté depuis plus de {days} jours"),
        )
    conn.commit()
    return count
