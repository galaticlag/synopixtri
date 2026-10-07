from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


LIVE_CANDIDATE_EXTENSIONS = {".mov"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".png"}
TEMPORAL_FALLBACK_SECONDS = 2


@dataclass(frozen=True)
class MediaRecord:
    path: Path
    sha: str
    size: int
    exif: dict[str, Any]
    reference_datetime: datetime
    gps: tuple[float, float] | None = None
    metadata_ok: bool = True


@dataclass(frozen=True)
class LogicalUnit:
    kind: str  # photo_single | video_single | live_pair | orphan_live_mov
    primary: MediaRecord
    live: MediaRecord | None
    reference_datetime: datetime


def _content_identifier(exif: dict[str, Any]) -> str | None:
    for key, value in exif.items():
        if "contentidentifier" in key.lower() and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_live_candidate(record: MediaRecord) -> bool:
    return record.path.suffix.lower() in LIVE_CANDIDATE_EXTENSIONS


def _is_photo(record: MediaRecord) -> bool:
    return record.path.suffix.lower() in PHOTO_EXTENSIONS


def build_logical_units(records: list[MediaRecord]) -> list[LogicalUnit]:
    photos = [r for r in records if _is_photo(r)]
    live_candidates = [r for r in records if _is_live_candidate(r)]
    others = [r for r in records if r not in photos and r not in live_candidates]

    unpaired_photos = list(photos)
    unpaired_movs = list(live_candidates)
    pairs: list[tuple[MediaRecord, MediaRecord]] = []

    # Step 1: exact basename match within the same parent folder.
    def _key(r: MediaRecord) -> tuple[str, str]:
        return (str(r.path.parent).lower(), r.path.stem.lower())

    photo_by_key: dict[tuple[str, str], MediaRecord] = {_key(p): p for p in unpaired_photos}
    still_unpaired_movs: list[MediaRecord] = []
    for mov in unpaired_movs:
        photo = photo_by_key.get(_key(mov))
        if photo is not None:
            pairs.append((photo, mov))
            unpaired_photos.remove(photo)
            del photo_by_key[_key(mov)]
        else:
            still_unpaired_movs.append(mov)
    unpaired_movs = still_unpaired_movs

    # Step 2: match by shared ContentIdentifier.
    photo_by_content_id: dict[str, list[MediaRecord]] = {}
    for p in unpaired_photos:
        cid = _content_identifier(p.exif)
        if cid:
            photo_by_content_id.setdefault(cid, []).append(p)

    still_unpaired_movs = []
    for mov in unpaired_movs:
        cid = _content_identifier(mov.exif)
        candidates = photo_by_content_id.get(cid) if cid else None
        if candidates:
            photo = candidates.pop(0)
            unpaired_photos.remove(photo)
            pairs.append((photo, mov))
            if not candidates:
                del photo_by_content_id[cid]
        else:
            still_unpaired_movs.append(mov)
    unpaired_movs = still_unpaired_movs

    # Step 3: temporal proximity fallback (< 2s, same folder).
    still_unpaired_movs = []
    for mov in unpaired_movs:
        best_photo: MediaRecord | None = None
        best_delta: float | None = None
        for photo in unpaired_photos:
            if photo.path.parent != mov.path.parent:
                continue
            delta = abs((photo.reference_datetime - mov.reference_datetime).total_seconds())
            if delta <= TEMPORAL_FALLBACK_SECONDS and (best_delta is None or delta < best_delta):
                best_photo = photo
                best_delta = delta
        if best_photo is not None:
            pairs.append((best_photo, mov))
            unpaired_photos.remove(best_photo)
        else:
            still_unpaired_movs.append(mov)
    unpaired_movs = still_unpaired_movs

    units: list[LogicalUnit] = []
    for photo, mov in pairs:
        units.append(
            LogicalUnit(
                kind="live_pair",
                primary=photo,
                live=mov,
                reference_datetime=photo.reference_datetime,
            )
        )
    for photo in unpaired_photos:
        units.append(
            LogicalUnit(
                kind="photo_single",
                primary=photo,
                live=None,
                reference_datetime=photo.reference_datetime,
            )
        )
    for mov in unpaired_movs:
        units.append(
            LogicalUnit(
                kind="orphan_live_mov",
                primary=mov,
                live=None,
                reference_datetime=mov.reference_datetime,
            )
        )
    for other in others:
        units.append(
            LogicalUnit(
                kind="video_single",
                primary=other,
                live=None,
                reference_datetime=other.reference_datetime,
            )
        )

    return units
