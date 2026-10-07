"""HTTP API and the single-page dashboard."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import __version__, analysis, config, db, folders, jobs, maintenance, naming, notify, rules, zones
from .scheduler import Scheduler

log = logging.getLogger("synopixtri")
_WEB = Path(__file__).parent / "web"
_basic = HTTPBasic(auto_error=False)
MASK = "********"


def create_app(boot: config.Bootstrap, *, start_scheduler: bool = True) -> FastAPI:
    scheduler = Scheduler(boot)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        jobs.recover(boot)
        if boot.password is None:
            log.warning("SYNOPIXTRI_PASSWORD is not set: the API is open to anyone who can reach it")
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="SynoPixtri", version=__version__, lifespan=lifespan)
    app.state.scheduler = scheduler

    def auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
        if boot.password is None:
            return
        ok = creds is not None and hmac.compare_digest(
            creds.password.encode(), boot.password.encode()
        )
        if not ok:
            raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Basic"})

    @contextmanager
    def session():
        boot.data_dir.mkdir(parents=True, exist_ok=True)
        c = db.connect(boot.db_path)
        db.init(c)
        try:
            yield c
        finally:
            c.close()

    def settings_and_paths(c):
        values = config.load(c)
        return config.namespace(values), config.resolve_paths(boot.photos_root, values)

    def bad(exc: Exception) -> HTTPException:
        return HTTPException(422, str(exc))

    @app.get("/", include_in_schema=False)
    def index(_: None = Depends(auth)):
        return FileResponse(_WEB / "index.html")

    # -- status, settings -------------------------------------------------------------------
    @app.get("/api/status")
    def status(_: None = Depends(auth)):
        with session() as c:
            values = config.load(c)
            last = c.execute("SELECT * FROM job ORDER BY id DESC LIMIT 1").fetchone()
            return {
                "version": __version__,
                "now": time.time(),
                "next_run_at": scheduler.next_run_at,
                "scheduler_error": scheduler.last_error,
                "inbox_files": c.execute("SELECT COUNT(*) FROM inbox_file").fetchone()[0],
                "last_job": jobs.job_dict(last) if last else None,
                "to_name": folders.to_name(c, str(values["placeholder_label"]), str(values["event_template"])),
                "paused_jobs": [r["id"] for r in c.execute("SELECT id FROM job WHERE state='paused_brake'")],
                "proposals": c.execute("SELECT COUNT(*) FROM proposal").fetchone()[0],
                "validation_mode": bool(values["validation_mode"]),
            }

    @app.get("/api/settings")
    def get_settings(_: None = Depends(auth)):
        with session() as c:
            values = config.load(c)
            for key in config.SECRET_KEYS:
                if values.get(key):
                    values[key] = MASK
            return values

    @app.put("/api/settings")
    def put_settings(updates: dict = Body(...), _: None = Depends(auth)):
        updates = {k: v for k, v in updates.items() if not (k in config.SECRET_KEYS and v == MASK)}
        with session() as c:
            try:
                values = config.save(c, updates)
            except config.SettingsError as exc:
                raise bad(exc) from exc
            for key in config.SECRET_KEYS:
                if values.get(key):
                    values[key] = MASK
            return values

    @app.post("/api/naming/preview")
    def naming_preview(body: dict = Body(...), _: None = Depends(auth)):
        event_tpl = str(body.get("event_template", naming.EVENT_DEFAULT))
        routine_tpl = str(body.get("routine_template", naming.ROUTINE_DEFAULT))
        label, place = str(body.get("label") or "Anniversaire"), str(body.get("place") or "Lyon")
        try:
            naming.validate_template("event", event_tpl)
            naming.validate_template("routine", routine_tpl)
        except ValueError as exc:
            raise bad(exc) from exc
        d = date(2026, 3, 14)
        examples = {
            "event": naming.event_name(d, d, label, place, event_tpl),
            "stay": naming.event_name(d, date(2026, 3, 16), label, place, event_tpl),
            "routine": naming.routine_name("2026-03", str(body.get("routine_label") or "Vie de famille"), routine_tpl),
        }
        parsed = naming.parse_name(examples["event"], "Vie de famille", event_tpl, routine_tpl)
        return {"examples": examples, "recognised": parsed is not None}

    # -- jobs -----------------------------------------------------------------------------------
    @app.get("/api/jobs")
    def list_jobs(limit: int = 30, _: None = Depends(auth)):
        with session() as c:
            rows = c.execute("SELECT * FROM job ORDER BY id DESC LIMIT ?", (min(limit, 200),)).fetchall()
            return [jobs.job_dict(r) for r in rows]

    @app.get("/api/jobs/{job_id}/operations")
    def operations(job_id: int, limit: int = 200, _: None = Depends(auth)):
        with session() as c:
            rows = c.execute(
                "SELECT seq, op, src, dst, state, reason, why, error FROM operation_log WHERE job_id=? "
                "ORDER BY seq LIMIT ?", (job_id, min(limit, 2000)),
            ).fetchall()  # fmt: skip
            return [dict(r) for r in rows]

    @app.post("/api/jobs/run")
    def run(dry_run: bool = False, _: None = Depends(auth)):
        try:
            scheduler.run_now(dry_run=dry_run)
        except jobs.Busy as exc:
            raise HTTPException(409, "a pass is already running") from exc
        return {"started": True, "dry_run": dry_run}

    @app.post("/api/jobs/{job_id}/confirm")
    def confirm(job_id: int, _: None = Depends(auth)):
        """Release a pass paused by the emergency brake: run again without the limit."""
        with session() as c:
            row = c.execute("SELECT state FROM job WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "paused_brake":
                raise HTTPException(404, "no paused job with this id")
            c.execute("UPDATE job SET state='confirmed' WHERE id=?", (job_id,))
            c.commit()
        try:
            scheduler.run_now(skip_brake=True)
        except jobs.Busy as exc:
            raise HTTPException(409, "a pass is already running") from exc
        return {"started": True}

    @app.post("/api/jobs/{job_id}/undo")
    def undo(job_id: int, _: None = Depends(auth)):
        try:
            return jobs.undo(boot, job_id)
        except jobs.Busy as exc:
            raise HTTPException(409, "a pass is running") from exc
        except KeyError as exc:
            raise HTTPException(404, "unknown job") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -- folders ----------------------------------------------------------------------------------
    @app.get("/api/folders")
    def list_folders(_: None = Depends(auth)):
        with session() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, path, role, status, date_start, date_end, missing FROM folder "
                "ORDER BY date_start DESC LIMIT 500"
            )]  # fmt: skip

    @app.put("/api/folders/{folder_id}")
    def lock_folder(folder_id: int, body: dict = Body(...), _: None = Depends(auth)):
        """Lock a folder (nothing is added to it any more) or release it."""
        wanted = body.get("locked")
        if not isinstance(wanted, bool):
            raise HTTPException(422, "locked must be true or false")
        with session() as c:
            row = c.execute("SELECT status FROM folder WHERE id=?", (folder_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "unknown folder")
            status = "verrouille" if wanted else ("utilisateur" if row["status"] == "verrouille" else row["status"])
            c.execute("UPDATE folder SET status=? WHERE id=?", (status, folder_id))
            c.commit()
            return {"id": folder_id, "status": status}

    # -- zones ------------------------------------------------------------------------------------
    @app.get("/api/zones")
    def get_zones(_: None = Depends(auth)):
        with session() as c:
            return zones.load(c)

    @app.post("/api/zones")
    def post_zone(body: dict = Body(...), _: None = Depends(auth)):
        with session() as c:
            try:
                zone = zones.create(c, body)
            except (ValueError, TypeError) as exc:
                raise bad(exc) from exc
            rules.snapshot(c, f"Zone « {zone['name']} » ajoutée")
            return zone

    @app.put("/api/zones/{zone_id}")
    def put_zone(zone_id: int, body: dict = Body(...), _: None = Depends(auth)):
        with session() as c:
            try:
                zone = zones.update(c, zone_id, body)
            except (ValueError, TypeError) as exc:
                raise bad(exc) from exc
            if zone is None:
                raise HTTPException(404, "unknown zone")
            rules.snapshot(c, f"Zone « {zone['name']} » modifiée")
            return zone

    @app.delete("/api/zones/{zone_id}")
    def delete_zone(zone_id: int, _: None = Depends(auth)):
        with session() as c:
            if not zones.delete(c, zone_id):
                raise HTTPException(404, "unknown zone")
            rules.snapshot(c, f"Zone n°{zone_id} supprimée")
            return {"deleted": zone_id}

    # -- rules ------------------------------------------------------------------------------------
    @app.get("/api/rules")
    def get_rules(_: None = Depends(auth)):
        with session() as c:
            return [{**r, "description": [rules.describe(x) for x in r["conditions"]]} for r in rules.load(c)]

    @app.get("/api/rules/templates")
    def rule_templates(_: None = Depends(auth)):
        return [{**t, "description": [rules.describe(x) for x in t["conditions"]]} for t in rules.TEMPLATES]

    @app.post("/api/rules/preview")
    def rule_preview(body: dict = Body(...), _: None = Depends(auth)):
        with session() as c:
            cfg, paths = settings_and_paths(c)
            try:
                return rules.preview(c, cfg, paths.inbox, body.get("conditions") or [])
            except (ValueError, TypeError) as exc:
                raise bad(exc) from exc

    @app.post("/api/rules/reorder")
    def reorder_rules(body: dict = Body(...), _: None = Depends(auth)):
        with session() as c:
            rules.reorder(c, [int(i) for i in body.get("ids") or []])
            rules.snapshot(c, "Ordre des règles modifié")
            return {"ok": True}

    @app.get("/api/rules/versions")
    def rule_versions(_: None = Depends(auth)):
        with session() as c:
            return rules.versions(c)

    @app.post("/api/rules/versions/{version_id}/restore")
    def restore_version(version_id: int, _: None = Depends(auth)):
        with session() as c:
            if not rules.restore(c, version_id):
                raise HTTPException(404, "unknown version")
            return {"restored": version_id}

    @app.post("/api/rules")
    def post_rule(body: dict = Body(...), _: None = Depends(auth)):
        with session() as c:
            try:
                rule = rules.create(c, body)
            except (ValueError, TypeError) as exc:
                raise bad(exc) from exc
            rules.snapshot(c, f"Règle « {rule['name']} » ajoutée")
            return rule

    @app.put("/api/rules/{rule_id}")
    def put_rule(rule_id: int, body: dict = Body(...), _: None = Depends(auth)):
        with session() as c:
            try:
                rule = rules.update(c, rule_id, body)
            except (ValueError, TypeError) as exc:
                raise bad(exc) from exc
            if rule is None:
                raise HTTPException(404, "unknown rule")
            rules.snapshot(c, f"Règle « {rule['name']} » modifiée")
            return rule

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int, _: None = Depends(auth)):
        with session() as c:
            if not rules.delete(c, rule_id):
                raise HTTPException(404, "unknown rule")
            rules.snapshot(c, f"Règle n°{rule_id} supprimée")
            return {"deleted": rule_id}

    # -- proposals (validation mode) ----------------------------------------------------------------
    @app.get("/api/proposals")
    def proposals(_: None = Depends(auth)):
        with session() as c:
            out = []
            for r in c.execute("SELECT * FROM proposal ORDER BY start_date"):
                paths = [p for p in json.loads(r["paths_json"]) if Path(p).exists()]
                out.append(
                    {
                        "id": r["id"], "start": r["start_date"], "end": r["end_date"], "lat": r["lat"],
                        "lon": r["lon"], "place": r["place"], "count": r["count"], "samples": paths[:12],
                    }
                )  # fmt: skip
            return out

    @app.get("/api/approvals")
    def approvals(_: None = Depends(auth)):
        with session() as c:
            return [dict(r) for r in c.execute("SELECT * FROM approval ORDER BY start_date")]

    @app.post("/api/approvals")
    def post_approvals(body: dict = Body(...), _: None = Depends(auth)):
        """Decisions on proposed groupings: rename (label/place), merge (wider range), split (several
        ranges) or keep in the monthly folder (action routine). The latest decision wins on overlaps."""
        items = body.get("items") or []
        clean = []
        for item in items:
            try:
                start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
            except (KeyError, ValueError, TypeError) as exc:
                raise bad(exc) from exc
            action = item.get("action", "event")
            if end < start or action not in ("event", "routine"):
                raise HTTPException(422, "invalid range or action")
            clean.append((start.isoformat(), end.isoformat(), action, (item.get("label") or "").strip() or None,
                          (item.get("place") or "").strip() or None))  # fmt: skip
        if not clean:
            raise HTTPException(422, "no decision given")
        with session() as c:
            for start, end, action, label, place in clean:
                c.execute("DELETE FROM approval WHERE start_date<=? AND end_date>=?", (end, start))
                c.execute(
                    "INSERT INTO approval(start_date, end_date, action, label, place, created_at) VALUES(?,?,?,?,?,?)",
                    (start, end, action, label, place, time.time()),
                )
            c.commit()
        return {"saved": len(clean)}

    @app.delete("/api/approvals/{approval_id}")
    def delete_approval(approval_id: int, _: None = Depends(auth)):
        with session() as c:
            c.execute("DELETE FROM approval WHERE id=?", (approval_id,))
            c.commit()
            return {"deleted": approval_id}

    # -- set-aside folder -----------------------------------------------------------------------------
    @app.get("/api/aside")
    def aside(_: None = Depends(auth)):
        with session() as c:
            _cfg, paths = settings_and_paths(c)
            return maintenance.list_aside(c, paths)

    @app.post("/api/aside/restore")
    def aside_restore(body: dict = Body(...), _: None = Depends(auth)):
        ids = [int(i) for i in body.get("ids") or []]
        if not ids:
            raise HTTPException(422, "no file selected")
        try:
            with jobs.exclusive():
                with session() as c:
                    _cfg, paths = settings_and_paths(c)
                    return maintenance.restore_aside(c, paths, ids)
        except jobs.Busy as exc:
            raise HTTPException(409, "a pass is running") from exc

    # -- duplicates, timeline, thumbnails --------------------------------------------------------------
    @app.get("/api/duplicates")
    def duplicates(_: None = Depends(auth)):
        with session() as c:
            cfg, paths = settings_and_paths(c)
            exact = []
            for r in c.execute(
                "SELECT o.id, o.src, o.dst, o.reason, o.why, j.started_at FROM operation_log o JOIN job j ON j.id=o.job_id "
                "WHERE o.op='move' AND o.state='done' AND o.reason IN ('duplicate_of_library','duplicate_in_batch') "
                "ORDER BY o.id DESC LIMIT 300"
            ):  # fmt: skip
                if r["dst"] and Path(r["dst"]).is_file():
                    exact.append({"id": r["id"], "name": Path(r["dst"]).name, "path": r["dst"], "origin": r["src"],
                                  "reason": r["reason"], "why": r["why"], "at": r["started_at"]})  # fmt: skip
            entries = [
                (r["path"], json.loads(r["analysis_json"]))
                for r in c.execute("SELECT path, analysis_json FROM inbox_file WHERE analysis_json IS NOT NULL")
            ]
            _flagged, groups = analysis.near_duplicates(entries, cfg.near_duplicate_distance)
            return {"exact": exact, "similar": groups, "analysis_on": bool(cfg.analysis_enabled or rules.needs_analysis(rules.load(c, enabled_only=True)))}

    @app.get("/api/timeline")
    def timeline(_: None = Depends(auth)):
        with session() as c:
            months: dict[str, dict] = {}
            for r in c.execute(
                "SELECT f.id, f.path, f.role, f.status, f.date_start, f.date_end, COUNT(p.id) AS n FROM folder f "
                "LEFT JOIN placed_file p ON p.folder_id=f.id WHERE f.missing=0 AND f.date_start IS NOT NULL "
                "GROUP BY f.id ORDER BY f.date_start DESC"
            ):  # fmt: skip
                key = r["date_start"][:7]
                month = months.setdefault(key, {"month": key, "count": 0, "folders": []})
                month["count"] += r["n"]
                month["folders"].append(
                    {"id": r["id"], "name": Path(r["path"]).name, "role": r["role"], "status": r["status"],
                     "start": r["date_start"], "end": r["date_end"], "count": r["n"]}
                )  # fmt: skip
            return sorted(months.values(), key=lambda m: m["month"], reverse=True)

    @app.get("/api/thumb")
    def thumb(path: str, _: None = Depends(auth)):
        root = boot.photos_root.resolve()
        try:
            target = Path(path).resolve()
        except OSError as exc:
            raise HTTPException(404, "not found") from exc
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, "not found")
        st = target.stat()
        cache = boot.data_dir / "thumbs" / (hashlib.sha1(f"{target}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest() + ".jpg")
        if cache.is_file():
            return Response(cache.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
        try:
            from PIL import Image, ImageOps

            with Image.open(target) as img:
                if img.format == "JPEG":
                    img.draft("RGB", (480, 480))
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((240, 240))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=70)
        except Exception as exc:  # noqa: BLE001 - HEIC, videos, broken files: no preview
            raise HTTPException(415, "no preview for this file") from exc
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(buf.getvalue())
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    # -- reminder --------------------------------------------------------------------------------------
    @app.post("/api/notify/test")
    def notify_test(_: None = Depends(auth)):
        with session() as c:
            cfg, _paths = settings_and_paths(c)
            return notify.check(c, cfg, force=True)

    return app
