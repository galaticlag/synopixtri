from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from synopixtri import config, db, jobs
from synopixtri.models import Meta

TZ = ZoneInfo("Europe/Paris")
# Synthetic coordinates only: nothing here may come from a real library.
PARIS = (48.8566, 2.3522)


def ts(year, month, day, hour=12, minute=0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp()


def photo(year, month, day, hour=10, minute=0, gps=PARIS, cid=None) -> Meta:
    return Meta(
        make="Apple", model="Test", content_id=cid, local_dt=datetime(year, month, day, hour, minute),
        date_source="exif", date_reliable=True, lat=gps[0] if gps else None, lon=gps[1] if gps else None,
    )  # fmt: skip


def video(year, month, day, hour=10, minute=0, gps=PARIS, cid=None, duration=2.5) -> Meta:
    m = photo(year, month, day, hour, minute, gps, cid)
    m.is_video, m.duration, m.date_source = True, duration, "keys_local"
    return m


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "photos"
        self.root.mkdir()
        self.boot = config.Bootstrap(photos_root=self.root, data_dir=tmp_path / "data")
        self.metas: dict[str, Meta] = {}
        self.place = "Testville"
        conn = self.conn()
        config.save(conn, {"event_min_items": 3, "away_event_min_items": 3, "geocode_enabled": False})
        conn.close()

    def conn(self):
        self.boot.data_dir.mkdir(parents=True, exist_ok=True)
        conn = db.connect(self.boot.db_path)
        db.init(conn)
        return conn

    def settings(self, **updates):
        conn = self.conn()
        config.save(conn, updates)
        conn.close()

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def library(self) -> Path:
        return self.root / "library"

    def add(self, name: str, meta: Meta | None, *, sub: str = "", content: bytes | None = None) -> Path:
        folder = self.inbox / sub if sub else self.inbox
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(content if content is not None else (str(path) + str(len(self.metas))).encode())
        os.utime(path, (1_600_000_000, 1_600_000_000))
        if meta is not None:
            self.metas[name] = meta
        return path

    def reader(self, paths):
        return {p: self.metas.get(p.name, Meta(error="no meta")) for p in paths}

    def run(self, now: float, **kwargs) -> dict:
        job_id = jobs.run_pass(
            self.boot, now=now, reader=self.reader, geocoder=lambda lat, lon: self.place, **kwargs
        )
        conn = self.conn()
        row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return {"id": job_id, **jobs.job_dict(row)}

    def settle(self, now: float, **kwargs) -> dict:
        """Two passes: the first sees the files, the second finds them stable."""
        self.run(now - 3 * 24 * 3600)
        return self.run(now, **kwargs)

    def tree(self, base: Path | None = None) -> list[str]:
        base = base or self.root
        return sorted(
            str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("*") if p.is_file()
        )


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)
