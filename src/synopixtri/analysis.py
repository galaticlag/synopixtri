"""Content analysis on small thumbnails with Pillow only. Results are suggestions: they never
move anything unless the user wrote a rule that uses them."""

from __future__ import annotations

import fnmatch
from pathlib import Path

ANALYZABLE = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
_THUMB = 256
SCREENSHOT_NAMES = ("screenshot*", "screen shot*", "capture d*cran*", "screen_*", "simulator screenshot*")
_LAPLACIAN = (0, 1, 0, 1, -4, 1, 0, 1, 0)
FLAGS = ("blur", "dark", "screenshot", "document", "near_duplicate")


def analyze(path: Path) -> dict | None:
    """Raw measurements of one image, or None if it cannot be decoded."""
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:  # Pillow is optional at runtime
        return None
    if path.suffix.lower() not in ANALYZABLE:
        return None
    try:
        with Image.open(path) as img:
            width, height = img.size
            if img.format == "JPEG":
                img.draft("RGB", (_THUMB * 2, _THUMB * 2))
            img = img.convert("RGB")
            img.thumbnail((_THUMB, _THUMB))
            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            lap = gray.filter(ImageFilter.Kernel((3, 3), _LAPLACIAN, scale=1, offset=128))
            lap = lap.crop((1, 1, lap.width - 1, lap.height - 1))  # Pillow leaves a 1 px border unfiltered
            hist = gray.histogram()
            total = float(sum(hist)) or 1.0
            sat = ImageStat.Stat(img.convert("HSV")).mean[1] / 255.0
            small = gray.resize((9, 8))
            px = list(small.getdata())
            bits = 0
            for row in range(8):
                for col in range(8):
                    bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
            return {
                "lap": round(ImageStat.Stat(lap).var[0], 2),
                "mean": round(stat.mean[0], 2),
                "std": round(stat.stddev[0], 2),
                "white": round(sum(hist[200:]) / total, 4),
                "sat": round(sat, 4),
                "dhash": f"{bits:016x}",
                "w": width,
                "h": height,
            }
    except Exception:  # noqa: BLE001 - a broken image simply has no analysis
        return None


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def flags(name: str, meta, analysis: dict | None, cfg) -> set[str]:
    """Suggestion flags of a file. ``near_duplicate`` is added by the caller (batch level)."""
    found: set[str] = set()
    lowered = name.lower()
    if any(fnmatch.fnmatch(lowered, p) for p in SCREENSHOT_NAMES):
        found.add("screenshot")
    camera = bool(meta and (meta.make or meta.model))
    if analysis:
        if analysis["lap"] < cfg.blur_threshold:
            found.add("blur")
        if analysis["mean"] < cfg.dark_threshold:
            found.add("dark")
        if analysis["white"] > 0.5 and analysis["sat"] < 0.15 and analysis["std"] > 40:
            found.add("document")
    if lowered.endswith(".png") and not camera and not (meta and meta.lat is not None):
        found.add("screenshot")
    return found


def near_duplicates(entries: list[tuple[str, dict]], distance: int) -> tuple[set[str], list[list[str]]]:
    """Groups of look-alike images. In each group the sharpest is kept, the others are flagged.

    ``entries`` are (key, analysis). Returns (flagged keys, groups of keys).
    """
    hashed = [(k, a) for k, a in entries if a and a.get("dhash")]
    parent = list(range(len(hashed)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(hashed)):
        for j in range(i + 1, len(hashed)):
            if hamming(hashed[i][1]["dhash"], hashed[j][1]["dhash"]) <= distance:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(hashed)):
        groups.setdefault(find(i), []).append(i)
    flagged: set[str] = set()
    result: list[list[str]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: (-hashed[i][1]["lap"], hashed[i][0]))
        result.append([hashed[i][0] for i in members])
        flagged.update(hashed[i][0] for i in members[1:])
    return flagged, result
