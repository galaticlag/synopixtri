"""Bootstrap configuration (environment) and user settings (stored in the database)."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace

from . import naming


@dataclass(frozen=True)
class Bootstrap:
    """What the container needs before the database exists."""

    photos_root: Path
    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8080
    password: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "synopixtri.db"


def load_bootstrap(env: dict[str, str] | None = None) -> Bootstrap:
    env = os.environ if env is None else env
    return Bootstrap(
        photos_root=Path(env.get("SYNOPIXTRI_PHOTOS_ROOT", "/photos")),
        data_dir=Path(env.get("SYNOPIXTRI_DATA_DIR", "/data")),
        host=env.get("SYNOPIXTRI_HOST", "0.0.0.0"),
        port=int(env.get("SYNOPIXTRI_PORT", "8080")),
        password=env.get("SYNOPIXTRI_PASSWORD") or None,
    )


# key -> default. The default's type drives validation (None means optional float).
DEFAULTS: dict[str, object] = {
    # Folders, relative to the photos root.
    "inbox_dir": "inbox",
    "library_dir": "library",
    "review_dir": "_a_revoir",
    "quarantine_dir": "_doublons",
    "aside_dir": "_mis_de_cote",
    # Scheduling.
    "timezone": "Europe/Paris",
    # Files without a capture date in their metadata: "auto" trusts the date in the file name, then the file
    # date when it is clearly older than the arrival (a copy keeps the copy date, which says nothing).
    "file_date_policy": "auto",
    "file_date_min_age_days": 2,
    "scan_interval_min": 30,
    "stability_min": 10,
    "active_hours": [],  # ["08:00-23:00"]; empty means always
    "run_on_startup": True,
    "startup_delay_s": 120,
    # Live Photos.
    "live_partner_wait_min": 60,
    "live_suffix": " - live",
    "orphan_live_policy": "live_subfolder",  # live_subfolder | review | video_single
    # Naming.
    "routine_label": "Vie de famille",
    "placeholder_label": "À nommer",
    "unknown_place": "Lieu inconnu",
    "event_template": naming.EVENT_DEFAULT,  # tokens: {range} {label} {place}
    "routine_template": naming.ROUTINE_DEFAULT,  # tokens: {year} {month} {label}
    # Grouping.
    "event_min_items": 20,
    "away_event_min_items": 10,
    "home_lat": None,
    "home_lon": None,
    "home_radius_km": 50.0,
    "stay_radius_km": 25.0,
    "max_gap_days_in_stay": 1,
    # Life cycle.
    "day_maturity_hours": 48,
    "stay_quiet_hours": 24,
    "stay_maturity_days": 1,
    "routine_immediate": True,
    "validation_mode": False,  # new events wait for approval instead of being created
    # Set-aside folder: permanent deletion is opt-in and needs the confirmation flag.
    "aside_purge_days": 0,
    "aside_purge_confirmed": False,
    # Content analysis (thumbnails, Pillow only). Suggestions only count through a user rule.
    "analysis_enabled": False,
    "analysis_max_per_pass": 200,
    "meta_budget_min": 10,
    "blur_threshold": 40.0,
    "dark_threshold": 45.0,
    "near_duplicate_distance": 4,
    # "To name" reminder.
    "notify_kind": "none",  # none | ntfy | webhook | email
    "notify_url": "",
    "notify_after_days": 3,
    "notify_every_days": 7,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_to": "",
    # Safety.
    "brake_max_files": 500,
    # Tools.
    "exiftool_path": "exiftool",
    "geocode_enabled": True,
    "geocode_language": "fr",
}

_CHOICES = {
    "orphan_live_policy": {"live_subfolder", "review", "video_single"},
    "notify_kind": {"none", "ntfy", "webhook", "email"},
    "file_date_policy": {"auto", "always", "never"},
}
_PATH_KEYS = ("inbox_dir", "library_dir", "review_dir", "quarantine_dir", "aside_dir")
SECRET_KEYS = ("smtp_password",)


class SettingsError(ValueError):
    pass


def _coerce(key: str, value: object) -> object:
    default = DEFAULTS[key]
    if key in ("home_lat", "home_lon"):
        return None if value in (None, "") else float(value)  # type: ignore[arg-type]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(value)  # type: ignore[arg-type]
    if isinstance(default, float):
        return float(value)  # type: ignore[arg-type]
    if isinstance(default, list):
        if not isinstance(value, list):
            raise SettingsError(f"{key} must be a list")
        for window in value:
            parse_window(str(window))
        return [str(v) for v in value]
    value = str(value)
    if key in ("event_template", "routine_template"):
        try:
            naming.validate_template("event" if key == "event_template" else "routine", value)
        except ValueError as exc:
            raise SettingsError(str(exc)) from exc
    if key in _CHOICES and value not in _CHOICES[key]:
        raise SettingsError(f"{key} must be one of {sorted(_CHOICES[key])}")
    return value


def load(conn: sqlite3.Connection) -> dict[str, object]:
    values = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM setting"):
        if row["key"] in DEFAULTS:
            values[row["key"]] = json.loads(row["value"])
    return values


def save(conn: sqlite3.Connection, updates: dict[str, object]) -> dict[str, object]:
    clean: dict[str, object] = {}
    for key, value in updates.items():
        if key not in DEFAULTS:
            raise SettingsError(f"unknown setting: {key}")
        try:
            clean[key] = _coerce(key, value)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"invalid value for {key}: {exc}") from exc
    for key in _PATH_KEYS:
        if key in clean:
            rel = Path(str(clean[key]))
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                raise SettingsError(f"{key} must be a relative path inside the photos root")
    for key, value in clean.items():
        conn.execute(
            "INSERT INTO setting(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
    conn.commit()
    merged = load(conn)
    _check_paths(merged)
    return merged


def _check_paths(values: dict[str, object]) -> None:
    dirs = [Path(str(values[k])) for k in _PATH_KEYS]
    for i, a in enumerate(dirs):
        for b in dirs[i + 1 :]:
            if a == b or a in b.parents or b in a.parents:
                raise SettingsError(f"folders must not overlap: {a} / {b}")


def namespace(values: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(**values)


@dataclass(frozen=True)
class Paths:
    root: Path
    inbox: Path
    library: Path
    review: Path
    quarantine: Path
    aside: Path


def resolve_paths(root: Path, values: dict[str, object]) -> Paths:
    root = root.resolve()

    def sub(key: str) -> Path:
        path = (root / str(values[key])).resolve()
        if path != root and root not in path.parents:
            raise SettingsError(f"{key} escapes the photos root")
        return path

    return Paths(
        root=root,
        inbox=sub("inbox_dir"),
        library=sub("library_dir"),
        review=sub("review_dir"),
        quarantine=sub("quarantine_dir"),
        aside=sub("aside_dir"),
    )


def parse_window(text: str) -> tuple[time, time]:
    try:
        start, end = text.split("-")
        return (
            datetime.strptime(start.strip(), "%H:%M").time(),
            datetime.strptime(end.strip(), "%H:%M").time(),
        )
    except ValueError as exc:
        raise SettingsError(f"invalid time window {text!r}, expected HH:MM-HH:MM") from exc


def in_active_hours(windows: list[str], local_now: datetime) -> bool:
    if not windows:
        return True
    now = local_now.time()
    for window in windows:
        start, end = parse_window(window)
        if start <= end:
            if start <= now <= end:
                return True
        elif now >= start or now <= end:  # crosses midnight
            return True
    return False
