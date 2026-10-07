"""Group inbox files into logical units: single media or Live Photo pairs."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from .models import LIVE_IMAGE_EXTS, Item, Unit

# A Live Photo video lasts about 2-3 seconds; this bounds the name-only pairing heuristic.
SHORT_LIVE_SECONDS = 6.0


def _is_live_like(item: Item) -> bool:
    m = item.meta
    if m is None:
        return False
    if m.content_id:
        return True
    return bool(m.duration is not None and m.duration <= SHORT_LIVE_SECONDS and m.make == "Apple")


def build_units(
    items: list[Item], cfg: SimpleNamespace, now: float
) -> tuple[list[Unit], list[tuple[Unit, str]]]:
    """Return (ready units, held units with the reason they must wait).

    Only stable files form units. An unstable file with the same name as a stable one is
    probably its Live partner still being uploaded, so the stable one waits.
    """
    wait_s = cfg.live_partner_wait_min * 60
    by_dir: dict[Path, list[Item]] = defaultdict(list)
    for item in items:
        by_dir[item.path.parent].append(item)

    ready: list[Unit] = []
    held: list[tuple[Unit, str]] = []

    for folder_items in by_dir.values():
        stable = [i for i in folder_items if i.stable]
        uploading = {i.stem for i in folder_items if not i.stable}
        images = [i for i in stable if i.ext in LIVE_IMAGE_EXTS]
        movs = [i for i in stable if i.ext == ".mov"]
        by_id = {i.meta.content_id: i for i in images if i.meta and i.meta.content_id}
        by_stem: dict[str, list[Item]] = defaultdict(list)
        for img in images:
            by_stem[img.stem].append(img)

        used: set[Path] = set()
        pairs: list[tuple[Item, Item]] = []
        for mov in movs:
            mid = mov.meta.content_id if mov.meta else None
            partner = by_id.get(mid) if mid else None
            if partner is None:
                for cand in by_stem.get(mov.stem, []):
                    cid = cand.meta.content_id if cand.meta else None
                    if mid and cid and mid != cid:
                        continue
                    short = bool(
                        mov.meta and mov.meta.duration is not None
                        and mov.meta.duration <= SHORT_LIVE_SECONDS
                    )
                    if mid or cid or short:
                        partner = cand
                        break
            if partner is not None and partner.path not in used:
                used.add(partner.path)
                used.add(mov.path)
                pairs.append((partner, mov))

        for img, mov in pairs:
            ready.append(Unit("live_pair", img, mov))

        for item in stable:
            if item.path in used:
                continue
            if item.ext in LIVE_IMAGE_EXTS or item.ext in {".png", ".webp", ".gif", ".tif", ".tiff", ".dng"}:
                unit = Unit("photo_single", item)
                wait = item.ext in LIVE_IMAGE_EXTS and _is_live_like(item)
            elif item.ext == ".mov" and _is_live_like(item):
                unit = Unit("orphan_live_mov", item)
                wait = True
            elif item.ext in {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mts"}:
                unit = Unit("video_single", item)
                wait = False
            else:
                unit = Unit("photo_single", item)
                wait = False
            if item.stem in uploading:
                held.append((unit, "partner_uploading"))
            elif wait and (now - item.last_change) < wait_s:
                held.append((unit, "waiting_live_partner"))
            else:
                ready.append(unit)
    return ready, held
