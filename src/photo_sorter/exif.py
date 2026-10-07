from __future__ import annotations

import json
import subprocess
from pathlib import Path


class ExifToolError(RuntimeError):
    pass


def exiftool_version(exiftool_path: str = "exiftool") -> str:
    try:
        proc = subprocess.run(
            [exiftool_path, "-ver"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise ExifToolError(
            f"ExifTool not found: '{exiftool_path}'. "
            "Install it (DietPi/Debian: `apt-get install libimage-exiftool-perl`) "
            "or set EXIFTOOL_PATH in config.yaml/.env."
        ) from e
    if proc.returncode != 0:
        raise ExifToolError(proc.stderr.strip() or "Failed to run exiftool -ver")
    return proc.stdout.strip()


def extract_metadata_json(
    file_paths: list[Path],
    *,
    exiftool_path: str = "exiftool",
) -> list[dict]:
    if not file_paths:
        return []

    args = [
        exiftool_path,
        "-j",
        "-G",
        "-n",
        "-time:all",
        "-gps:all",
        "-composite:all",
        "-quicktime:all",
        "-file:all",
        "-Common:all",
        "-api",
        "LargeFileSupport=1",
    ]
    args.extend(str(p) for p in file_paths)

    try:
        proc = subprocess.run(args, check=False, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ExifToolError(
            f"ExifTool not found: '{exiftool_path}'. "
            "Install it (DietPi/Debian: `apt-get install libimage-exiftool-perl`) "
            "or set EXIFTOOL_PATH in config.yaml/.env."
        ) from e

    # returncode=1 means some files had warnings/errors but others were processed;
    # try to use the partial JSON output before giving up.
    if proc.returncode not in (0, 1) and not proc.stdout.strip():
        raise ExifToolError(proc.stderr.strip() or "ExifTool failed")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        # If we have no usable output at all, surface the real ExifTool error.
        err = proc.stderr.strip() or "ExifTool failed"
        raise ExifToolError(f"{err} (JSON parse: {e})") from e

    if not isinstance(data, list):
        raise ExifToolError("Unexpected ExifTool JSON format")

    return data
