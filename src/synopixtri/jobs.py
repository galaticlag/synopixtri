"""One sorting pass from end to end, and the job records around it."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Callable
from zoneinfo import ZoneInfo

from . import analysis, config, db, duplicates, exif, folders, geo, maintenance, notify, plan as planner, rules, scan, units
from .executor import Executor, undo_job
from .models import SIDECAR_EXTS, Meta

log = logging.getLogger("synopixtri")
_LOCK = threading.Lock()

Reader = Callable[[list], "dict[object, Meta]"]


class Busy(Exception):
    """Another pass or undo is already running."""


@contextmanager
def exclusive():
    """Hold the pass lock for a short operation (restore, ...). Raises Busy if a pass is running."""
    if not _LOCK.acquire(blocking=False):
        raise Busy()
    try:
        yield
    finally:
        _LOCK.release()


def _open(boot: config.Bootstrap) -> sqlite3.Connection:
    boot.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(boot.db_path)
    db.init(conn)
    return conn


def recover(boot: config.Bootstrap) -> None:
    """A job still 'running' at startup was interrupted: flag it so it can be undone."""
    conn = _open(boot)
    conn.execute("UPDATE job SET state='failed', error='interrupted', finished_at=? WHERE state='running'", (time.time(),))
    conn.commit()
    conn.close()


def run_pass(
    boot: config.Bootstrap,
    *,
    dry_run: bool = False,
    skip_brake: bool = False,
    now: float | None = None,
    reader: Reader | None = None,
    geocoder: planner.Geocoder | None = None,
) -> int:
    if not _LOCK.acquire(blocking=False):
        raise Busy()
    try:
        return _run(boot, dry_run, skip_brake, time.time() if now is None else now, reader, geocoder)
    finally:
        _LOCK.release()


def _run(boot, dry_run, skip_brake, now, reader, geocoder) -> int:
    conn = _open(boot)
    try:
        values = config.load(conn)
        cfg = config.namespace(values)
        paths = config.resolve_paths(boot.photos_root, values)
        tz = ZoneInfo(cfg.timezone)
        cur = conn.execute(
            "INSERT INTO job(kind, state, dry_run, started_at) VALUES('pass', 'running', ?, ?)",
            (int(dry_run), now),
        )
        job_id = int(cur.lastrowid)
        conn.commit()
        try:
            stats = _pass(conn, job_id, cfg, paths, tz, now, dry_run, skip_brake, reader, geocoder)
            state = stats.pop("_state")
            conn.execute(
                "UPDATE job SET state=?, finished_at=?, stats_json=? WHERE id=?",
                (state, time.time(), json.dumps(stats), job_id),
            )
            if not dry_run:  # a newer real pass replaces any older pass still waiting for confirmation
                conn.execute("UPDATE job SET state='superseded' WHERE state='paused_brake' AND id<?", (job_id,))
            conn.commit()
            if not dry_run and state in ("done", "done_with_errors"):
                notify.check(conn, cfg, now)
        except Exception as exc:  # noqa: BLE001 - recorded on the job, then re-raised
            log.exception("pass failed")
            conn.execute(
                "UPDATE job SET state='failed', finished_at=?, error=? WHERE id=?",
                (time.time(), f"{type(exc).__name__}: {exc}", job_id),
            )
            conn.commit()
            raise
        conn.commit()
        return job_id
    finally:
        conn.close()


_META_CHUNK = 200


def _set_progress(conn, value: dict | None) -> None:
    if value is None:
        conn.execute("DELETE FROM kv WHERE key='progress'")
    else:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES('progress', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(value),),
        )
    conn.commit()


def _read_metadata(conn, need: list, read, budget_s: float) -> None:
    """Read metadata chunk by chunk and save each chunk at once: a long first pass (tens of thousands of
    files on a small NAS) shows its progress, survives an interruption and stops after ``budget_s``
    seconds, the rest being read by the next pass."""
    started = time.time()
    for start in range(0, len(need), _META_CHUNK):
        if start and time.time() - started > budget_s:
            break
        chunk = need[start : start + _META_CHUNK]
        metas = read([i.path for i in chunk])
        for item in chunk:
            item.meta = metas.get(item.path) or Meta(error="no metadata")
            scan.store_meta(conn, item)
        _set_progress(conn, {"phase": "metadata", "done": min(start + _META_CHUNK, len(need)), "total": len(need)})


def _pass(conn, job_id, cfg, paths, tz, now, dry_run, skip_brake, reader, geocoder) -> dict:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.library.mkdir(parents=True, exist_ok=True)

    renamed = folders.reconcile(conn, paths.library)
    adopted = folders.index_library(conn, paths.library, cfg)

    items = scan.scan_inbox(conn, paths.inbox, now, cfg.stability_min * 60)
    read = reader or (lambda ps: exif.read_metadata(ps, cfg.exiftool_path, tz))
    need = [i for i in items if i.stable and i.meta is None]
    for item in [i for i in need if i.ext in SIDECAR_EXTS]:
        item.meta = Meta(mime="sidecar")  # nothing to read: only a rule can place these
        scan.store_meta(conn, item)
    need = [i for i in need if i.ext not in SIDECAR_EXTS]
    _read_metadata(conn, need, read, cfg.meta_budget_min * 60)
    _set_progress(conn, None)
    conn.commit()
    for item in items:
        if item.meta is not None:
            exif.refine_date(item.meta, item.path.name, item.mtime_ns, item.first_seen, cfg.file_date_policy,
                             cfg.file_date_min_age_days, now, tz)
    unread = [i for i in items if i.stable and i.meta is None]
    items = [i for i in items if i not in unread]  # read at the next pass, never planned half-known

    rule_list = rules.load(conn, enabled_only=True)
    analysed = _analyse(conn, cfg, items, rules.needs_analysis(rule_list))

    ready, held = units.build_units(items, cfg, now)
    dups = duplicates.find_duplicates(conn, ready, paths.library)
    near: set[str] = set()
    if analysed is not None:
        flagged, _groups = analysis.near_duplicates(
            [(u.key, u.primary.analysis) for u in ready if u.primary.analysis], cfg.near_duplicate_distance
        )
        near = flagged
    exempt = {r["path"] for r in conn.execute("SELECT path FROM exempt")}
    geocode = geocoder or (
        lambda lat, lon: geo.reverse_geocode(conn, lat, lon, cfg.geocode_language, cfg.geocode_enabled)
    )
    plan = planner.build_plan(conn, cfg, paths, ready, now, tz, geocode, dups, near, exempt)
    plan.held += [(u.key, reason) for u, reason in held]

    stats: dict = {
        "inbox_files": len(items),
        "stable_files": sum(1 for i in items if i.stable),
        "unstable_files": sum(1 for i in items if not i.stable),
        "unread_files": len(unread),
        "stability_min": cfg.stability_min,
        "units": len(ready),
        "folders_renamed_by_user": renamed,
        "folders_adopted": adopted,
        "planned_moves": len(plan.moves),
        "by_reason": dict(Counter(m.reason for m in plan.moves)),
        "held": dict(Counter(reason for _key, reason in plan.held)),
        "rules_version": rules.latest_version(conn),
        "analysed": analysed or 0,
        "proposals": len(plan.proposals),
    }
    if not dry_run and not skip_brake and len(plan.moves) > cfg.brake_max_files:
        stats["_state"] = "paused_brake"
        stats["brake_limit"] = cfg.brake_max_files
        return stats

    executor = Executor(conn, job_id, cfg, dry_run)
    stats.update(executor.run(plan))
    if not dry_run:
        maintenance.save_proposals(conn, plan.proposals, now)
        maintenance.clean_exempt(conn)
    stats["purged"] = maintenance.purge_aside(conn, job_id, cfg, paths, now, dry_run)
    stats["_state"] = "dry_run" if dry_run else ("done_with_errors" if stats.get("failed") else "done")
    return stats


def _analyse(conn, cfg, items, wanted: bool) -> int | None:
    """Pillow measurements on stable images, a few per pass. None when analysis is not needed."""
    if not (wanted or cfg.analysis_enabled):
        return None
    done = 0
    for item in items:
        if item.analysis is not None or not item.stable or item.ext not in analysis.ANALYZABLE:
            continue
        if done >= cfg.analysis_max_per_pass:
            break
        item.analysis = analysis.analyze(item.path)
        done += 1
        if item.analysis is not None:
            scan.store_analysis(conn, item)
    conn.commit()
    return done


def undo(boot: config.Bootstrap, job_id: int) -> dict:
    if not _LOCK.acquire(blocking=False):
        raise Busy()
    conn = _open(boot)
    try:
        row = conn.execute("SELECT state FROM job WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["state"] not in ("done", "done_with_errors", "failed"):
            raise ValueError(f"job {job_id} is {row['state']}, nothing to undo")
        result = undo_job(conn, job_id)
        state = "undone" if not result["conflicts"] and not result["missing"] else "partial_undo"
        conn.execute("UPDATE job SET state=? WHERE id=?", (state, job_id))
        conn.commit()
        return {**result, "state": state}
    finally:
        conn.close()
        _LOCK.release()


def job_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        "dry_run": bool(row["dry_run"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "stats": json.loads(row["stats_json"]) if row["stats_json"] else None,
        "error": row["error"],
    }
