from __future__ import annotations

from pathlib import Path


DEFAULT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".png",
    ".mov",
    ".mp4",
}


def scan_media_files(root: Path, *, extensions: set[str] | None = None) -> list[Path]:
    exts = {e.lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    root = root.expanduser()

    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in exts:
            files.append(p)

    files.sort()
    return files
