"""Metadata extraction through ExifTool, and the reference-date rules."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import VIDEO_EXTS, Meta

TAGS = [
    "Make", "Model", "ContentIdentifier", "ImageWidth", "ImageHeight", "Duration",
    "MIMEType", "DateTimeOriginal", "CreateDate", "OffsetTimeOriginal", "CreationDate",
    "MediaCreateDate", "TrackCreateDate", "GPSLatitude", "GPSLongitude", "FileModifyDate",
]  # fmt: skip
_CHUNK = 100

_DT = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_dt(value: object) -> tuple[datetime, timedelta | None] | None:
    """Parse an ExifTool date. Returns (naive wall-clock datetime, UTC offset or None)."""
    if not isinstance(value, str):
        return None
    m = _DT.match(value.strip())
    if not m or int(m.group(1)) == 0:
        return None
    try:
        dt = datetime(*(int(m.group(i)) for i in range(1, 7)))
    except ValueError:
        return None
    zone = m.group(7)
    if zone is None:
        return dt, None
    if zone == "Z":
        return dt, timedelta(0)
    sign = 1 if zone[0] == "+" else -1
    digits = zone[1:].replace(":", "")
    return dt, sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))


def _utc_to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)


def select_reference_date(
    rec: dict, is_video: bool, tz: ZoneInfo
) -> tuple[datetime | None, str, bool]:
    """Pick the capture date as a naive local datetime.

    Photos: EXIF dates are already local. Videos: ``Keys:CreationDate`` is local with an
    offset (iPhone); the QuickTime dates are UTC and must be converted. The file date is
    only a last resort and is flagged unreliable (it changes when files are copied).
    """
    if is_video:
        local = parse_dt(rec.get("CreationDate"))
        if local and local[1] is not None:
            return local[0], "keys_local", True
        for tag in ("CreateDate", "MediaCreateDate", "TrackCreateDate"):
            parsed = parse_dt(rec.get(tag))
            if parsed:
                return _utc_to_local(parsed[0], tz), "quicktime_utc", True
        if local:
            return local[0], "keys_naive", True
    else:
        for tag in ("DateTimeOriginal", "CreateDate"):
            parsed = parse_dt(rec.get(tag))
            if parsed:
                return parsed[0], "exif", True
    fallback = parse_dt(rec.get("FileModifyDate"))
    if fallback:
        return fallback[0], "file", False
    return None, "none", False


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_record(path: Path, rec: dict, tz: ZoneInfo) -> Meta:
    if rec.get("Error"):
        return Meta(error=str(rec["Error"]))
    is_video = path.suffix.lower() in VIDEO_EXTS or str(rec.get("MIMEType", "")).startswith("video")
    local_dt, source, reliable = select_reference_date(rec, is_video, tz)
    lat, lon = _num(rec.get("GPSLatitude")), _num(rec.get("GPSLongitude"))
    if lat is None or lon is None or (lat == 0 and lon == 0):
        lat = lon = None
    content_id = rec.get("ContentIdentifier")
    return Meta(
        make=rec.get("Make"),
        model=rec.get("Model"),
        content_id=str(content_id) if content_id else None,
        width=int(rec["ImageWidth"]) if isinstance(rec.get("ImageWidth"), (int, float)) else None,
        height=int(rec["ImageHeight"]) if isinstance(rec.get("ImageHeight"), (int, float)) else None,
        duration=_num(rec.get("Duration")),
        mime=rec.get("MIMEType"),
        is_video=is_video,
        local_dt=local_dt,
        date_source=source,
        date_reliable=reliable,
        lat=lat,
        lon=lon,
    )


def read_metadata(paths: list[Path], exiftool: str, tz: ZoneInfo) -> dict[Path, Meta]:
    """Read metadata for ``paths`` in batches. A file ExifTool cannot read gets ``Meta.error``."""
    result: dict[Path, Meta] = {}
    for start in range(0, len(paths), _CHUNK):
        chunk = paths[start : start + _CHUNK]
        cmd = [exiftool, "-j", "-n", "-charset", "filename=utf8", *[f"-{t}" for t in TAGS]]
        cmd += [str(p) for p in chunk]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            for p in chunk:
                result[p] = Meta(error=f"exiftool failed: {exc}")
            continue
        records: dict[str, dict] = {}
        if proc.stdout.strip():
            try:
                for rec in json.loads(proc.stdout.decode("utf-8", "replace")):
                    records[_norm(rec.get("SourceFile", ""))] = rec
            except json.JSONDecodeError:
                pass
        for p in chunk:
            rec = records.get(_norm(str(p)))
            if rec is None:
                result[p] = Meta(error="exiftool returned no data for this file")
            else:
                result[p] = parse_record(p, rec, tz)
    return result


def _norm(path: str) -> str:
    return path.replace("\\", "/").lower()
