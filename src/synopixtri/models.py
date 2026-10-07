"""Plain data structures shared by the pipeline stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".gif", ".tif", ".tiff", ".dng"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mts"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
# Phone leftovers: scanned so a rule can set them aside, never read with ExifTool.
SIDECAR_EXTS = {".aae", ".thm"}
# Stills that an iPhone can pair with a Live Photo video.
LIVE_IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif"}


@dataclass
class Meta:
    """Metadata of one media file, reduced to what the sorter needs."""

    make: str | None = None
    model: str | None = None
    content_id: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    mime: str | None = None
    is_video: bool = False
    # Reference date as a naive *local* datetime, see exif.select_reference_date.
    local_dt: datetime | None = None
    date_source: str = "none"
    date_reliable: bool = False
    lat: float | None = None
    lon: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["local_dt"] = self.local_dt.isoformat() if self.local_dt else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Meta":
        data = dict(data)
        if data.get("local_dt"):
            data["local_dt"] = datetime.fromisoformat(data["local_dt"])
        return cls(**data)


@dataclass
class Item:
    """A media file sitting in the inbox."""

    path: Path
    size: int
    mtime_ns: int
    first_seen: float
    last_change: float
    stable: bool = False
    meta: Meta | None = None
    analysis: dict | None = None

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def stem(self) -> str:
        return self.path.stem.lower()


@dataclass
class Unit:
    """One logical media unit: a single file or a Live Photo pair."""

    kind: str  # photo_single | video_single | live_pair | orphan_live_mov
    primary: Item
    live: Item | None = None

    @property
    def key(self) -> str:
        return str(self.primary.path)

    @property
    def files(self) -> list[Item]:
        return [self.primary] + ([self.live] if self.live else [])

    @property
    def meta(self) -> Meta | None:
        return self.primary.meta

    @property
    def ref_dt(self) -> datetime | None:
        return self.meta.local_dt if self.meta else None

    @property
    def reliable(self) -> bool:
        return bool(self.meta and self.meta.local_dt and self.meta.date_reliable)

    @property
    def gps(self) -> tuple[float, float] | None:
        for item in self.files:
            m = item.meta
            if m and m.lat is not None and m.lon is not None:
                return (m.lat, m.lon)
        return None

    @property
    def last_arrival(self) -> float:
        return max(i.last_change for i in self.files)
