"""Apply a plan (move files, create/rename folders) with a journal, and undo a job."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from . import scan
from .plan import Move, Plan, Target


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 10000):
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"no free name for {path}")


def move_file(src: Path, dst: Path) -> None:
    """Move without ever overwriting. Cross-device moves are copy + verify + delete."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        same_device = os.stat(src).st_dev == os.stat(dst.parent).st_dev
    except OSError:
        same_device = False
    if same_device:
        os.replace(src, dst)
        return
    shutil.copy2(src, dst)
    if dst.stat().st_size != src.stat().st_size:
        dst.unlink(missing_ok=True)
        raise OSError("size mismatch after copy")
    src.unlink()


class Executor:
    def __init__(self, conn: sqlite3.Connection, job_id: int, cfg: SimpleNamespace, dry_run: bool) -> None:
        self.conn, self.job_id, self.cfg, self.dry_run = conn, job_id, cfg, dry_run
        self.seq = 0
        self.stats = {"moved": 0, "failed": 0, "folders_created": 0, "folders_renamed": 0}
        self._final: dict[int, Path] = {}  # id(target) -> directory actually used

    # -- journal -----------------------------------------------------------------------------
    def _log(self, op: str, src: Path | None, dst: Path | None, state: str, reason: str = "",
             unit_key: str | None = None, meta: dict | None = None, why: str | None = None) -> int:  # fmt: skip
        self.seq += 1
        cur = self.conn.execute(
            "INSERT INTO operation_log(job_id, seq, op, src, dst, state, reason, unit_key, meta_json, why) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.job_id, self.seq, op, str(src) if src else None, str(dst) if dst else None, state,
             reason, unit_key, json.dumps(meta) if meta else None, why or None),
        )  # fmt: skip
        return int(cur.lastrowid)

    def _set(self, op_id: int, state: str, error: str | None = None, dst: Path | None = None,
             meta: dict | None = None) -> None:  # fmt: skip
        self.conn.execute(
            "UPDATE operation_log SET state=?, error=?, dst=COALESCE(?, dst), meta_json=COALESCE(?, meta_json) "
            "WHERE id=?",
            (state, error, str(dst) if dst else None, json.dumps(meta) if meta else None, op_id),
        )

    # -- folders -----------------------------------------------------------------------------
    def _live_dir(self, folder: Path) -> Path:
        suffix = self.cfg.live_suffix
        if folder.is_dir():
            for child in sorted(folder.iterdir()):
                if child.is_dir() and child.name.endswith(suffix):
                    return child
        return folder / f"{folder.name}{suffix}"

    def _mkdirs(self, directory: Path) -> bool:
        """Create ``directory`` and missing parents, one journal entry per level (so undo can remove them)."""
        missing: list[Path] = []
        probe = directory
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        for level in reversed(missing):
            op = self._log("mkdir", None, level, "pending", "new_folder")
            level.mkdir(exist_ok=True)
            self._set(op, "done")
        return bool(missing)

    def _ensure(self, target: Target) -> Path:
        """Create or rename the target folder; return the directory to use."""
        key = id(target)
        if key in self._final:
            return self._final[key]
        folder = target.path
        if not target.tracked:
            self._final[key] = folder
            return folder
        if target.rename_to and target.row_id is not None:
            new_path = folder.with_name(target.rename_to)
            if folder.is_dir() and not new_path.exists():
                op = self._log("rename_dir", folder, new_path, "pending", "widen_range",
                               meta={"folder_id": target.row_id})  # fmt: skip
                old_live = self._live_dir(folder)
                os.rename(folder, new_path)
                if old_live.parent == folder and old_live.name == f"{folder.name}{self.cfg.live_suffix}":
                    renamed_live = new_path / f"{new_path.name}{self.cfg.live_suffix}"
                    if (new_path / old_live.name).is_dir() and not renamed_live.exists():
                        os.rename(new_path / old_live.name, renamed_live)
                prev = self.conn.execute("SELECT * FROM folder WHERE id=?", (target.row_id,)).fetchone()
                self._set(op, "done", meta={"folder_id": target.row_id, "prev": {
                    "path": prev["path"], "managed_name": prev["managed_name"], "role": prev["role"],
                    "date_start": prev["date_start"], "date_end": prev["date_end"]}})  # fmt: skip
                role = "evenement" if target.date_start == target.date_end else "sejour"
                self.conn.execute(
                    "UPDATE folder SET path=?, managed_name=?, role=?, date_start=?, date_end=? WHERE id=?",
                    (str(new_path), new_path.name, role, target.date_start.isoformat() if target.date_start else None,
                     target.date_end.isoformat() if target.date_end else None, target.row_id),
                )  # fmt: skip
                self.stats["folders_renamed"] += 1
                folder = new_path
                target.path = new_path
        if target.row_id is None:
            row = self.conn.execute("SELECT * FROM folder WHERE path=?", (str(folder),)).fetchone()
            if row is not None:
                target.row_id = row["id"]
            else:
                created = not folder.exists()
                if created:
                    self._mkdirs(folder)
                    self.stats["folders_created"] += 1
                status = "gere" if created else "utilisateur"
                cur = self.conn.execute(
                    "INSERT INTO folder(path, role, status, managed_name, date_start, date_end, lat, lon, "
                    "month_key, created_job_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (str(folder), target.role, status, folder.name if created else None,
                     target.date_start.isoformat() if target.date_start else None,
                     target.date_end.isoformat() if target.date_end else None,
                     target.lat, target.lon, target.month_key, self.job_id if created else None),
                )  # fmt: skip
                target.row_id = int(cur.lastrowid)
        self._final[key] = folder
        return folder

    # -- moves -------------------------------------------------------------------------------
    def run(self, plan: Plan) -> dict:
        if self.dry_run:
            for move in plan.moves:
                dest = move.target.path
                if move.target.tracked and move.live:
                    dest = dest / f"{dest.name}{self.cfg.live_suffix}"
                self._log("move", move.src, dest / move.src.name, "planned", move.reason, move.unit_key, why=move.why)
            self.stats["planned"] = len(plan.moves)
            self.conn.commit()
            return self.stats
        for target in plan.targets():
            if any(m.target is target for m in plan.moves):
                try:
                    self._ensure(target)
                except OSError as exc:
                    self.stats["failed"] += 1
                    self._log("mkdir", None, target.path, "failed", "folder_error", meta=None)
                    self.conn.execute("UPDATE operation_log SET error=? WHERE id=last_insert_rowid()", (str(exc),))
        for move in plan.moves:
            self._apply(move)
        self.conn.commit()
        return self.stats

    def _apply(self, move: Move) -> None:
        target = move.target
        folder = self._final.get(id(target))
        if folder is None:
            self.stats["failed"] += 1
            return
        dest_dir = self._live_dir(folder) if (target.tracked and move.live) else folder
        src = move.src
        if move.placed_id is not None:  # promotion: the file may have moved with a renamed folder
            row = self.conn.execute(
                "SELECT p.rel_path, f.path AS fp, p.folder_id FROM placed_file p JOIN folder f ON f.id=p.folder_id "
                "WHERE p.id=?", (move.placed_id,),
            ).fetchone()  # fmt: skip
            if row is None:
                return
            src = Path(row["fp"]) / row["rel_path"]
        if not src.exists():
            self._log("move", src, None, "failed", move.reason, move.unit_key)
            self.conn.execute("UPDATE operation_log SET error='source missing' WHERE id=last_insert_rowid()")
            self.stats["failed"] += 1
            return
        self._mkdirs(dest_dir)
        dst = unique_path(dest_dir / src.name)
        meta: dict = {}
        if move.placed_id is not None:
            meta = {"placed_id": move.placed_id, "prev_folder_id": row["folder_id"], "prev_rel": row["rel_path"]}
        op = self._log("move", src, dst, "pending", move.reason, move.unit_key, meta or None, move.why)
        try:
            move_file(src, dst)
        except OSError as exc:
            self._set(op, "failed", str(exc))
            self.stats["failed"] += 1
            return
        self.stats["moved"] += 1
        if move.placed_id is None:
            scan.forget(self.conn, src)
        if target.tracked and target.row_id is not None:
            st = dst.stat()
            rel = str(dst.relative_to(folder)).replace("\\", "/")
            if move.placed_id is not None:
                self.conn.execute(
                    "UPDATE placed_file SET folder_id=?, rel_path=?, dev=?, ino=?, job_id=? WHERE id=?",
                    (target.row_id, rel, st.st_dev, st.st_ino, self.job_id, move.placed_id),
                )
                placed_id = move.placed_id
            else:
                cur = self.conn.execute(
                    "INSERT INTO placed_file(folder_id, rel_path, dev, ino, size, mtime_ns, ref_date, unit_key, "
                    "is_live, content_id, job_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (target.row_id, rel, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                     move.ref_date.isoformat() if move.ref_date else None, move.unit_key, int(move.live),
                     move.content_id, self.job_id),
                )  # fmt: skip
                placed_id = int(cur.lastrowid)
                meta = {"placed_new": placed_id}
            self._widen(target.row_id, move.ref_date)
            self._set(op, "done", dst=dst, meta={**meta, "placed_id": placed_id} if meta else {"placed_id": placed_id})
        else:
            self._set(op, "done", dst=dst)

    def _widen(self, folder_id: int, day) -> None:
        if day is None:
            return
        iso = day.isoformat()
        self.conn.execute(
            "UPDATE folder SET date_start=CASE WHEN date_start IS NULL OR date_start>? THEN ? ELSE date_start END, "
            "date_end=CASE WHEN date_end IS NULL OR date_end<? THEN ? ELSE date_end END "
            "WHERE id=? AND role IN ('evenement','sejour')",
            (iso, iso, iso, iso, folder_id),
        )


def undo_job(conn: sqlite3.Connection, job_id: int) -> dict:
    """Put every file of a job back where it was. Reports what could not be restored."""
    ops = conn.execute(
        "SELECT * FROM operation_log WHERE job_id=? AND state IN ('done','pending') ORDER BY seq DESC",
        (job_id,),
    ).fetchall()
    result = {"restored": 0, "conflicts": [], "missing": []}
    for op in ops:
        kind, src, dst = op["op"], op["src"], op["dst"]
        meta = json.loads(op["meta_json"]) if op["meta_json"] else {}
        if kind == "move":
            if dst and Path(dst).exists():
                if Path(src).exists():
                    result["conflicts"].append(src)
                    conn.execute("UPDATE operation_log SET state='undo_failed', error='source exists' WHERE id=?", (op["id"],))
                    continue
                try:
                    move_file(Path(dst), Path(src))
                except OSError as exc:
                    conn.execute("UPDATE operation_log SET state='undo_failed', error=? WHERE id=?", (str(exc), op["id"]))
                    result["conflicts"].append(src)
                    continue
                result["restored"] += 1
                if meta.get("prev_folder_id") is not None:
                    conn.execute(
                        "UPDATE placed_file SET folder_id=?, rel_path=? WHERE id=?",
                        (meta["prev_folder_id"], meta["prev_rel"], meta["placed_id"]),
                    )
                elif meta.get("placed_new"):
                    conn.execute("DELETE FROM placed_file WHERE id=?", (meta["placed_new"],))
                conn.execute("UPDATE operation_log SET state='undone' WHERE id=?", (op["id"],))
            else:
                result["missing"].append(dst or src)
                conn.execute("UPDATE operation_log SET state='undo_failed', error='destination missing' WHERE id=?", (op["id"],))
        elif kind == "mkdir":
            try:
                Path(dst).rmdir()
                conn.execute("DELETE FROM folder WHERE path=? AND created_job_id=?", (dst, job_id))
                conn.execute("UPDATE operation_log SET state='undone' WHERE id=?", (op["id"],))
            except OSError:
                pass  # not empty or already gone: keep the folder
        elif kind == "rename_dir":
            if dst and Path(dst).is_dir() and not Path(src).exists():
                os.rename(dst, src)
                prev = meta.get("prev", {})
                if prev:
                    conn.execute(
                        "UPDATE folder SET path=?, managed_name=?, role=?, date_start=?, date_end=? WHERE id=?",
                        (prev["path"], prev["managed_name"], prev["role"], prev["date_start"], prev["date_end"],
                         meta["folder_id"]),
                    )  # fmt: skip
                conn.execute("UPDATE operation_log SET state='undone' WHERE id=?", (op["id"],))
    conn.commit()
    return result
