from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field


MoveMode = Literal["copy", "move"]


class PathsConfig(BaseModel):
    source_root: Path
    target_root: Path
    output_root: Path
    quarantine_root: Path
    review_root: Path


class AppConfig(BaseModel):
    # Paths
    paths: PathsConfig

    # Execution defaults
    move_mode: MoveMode = "copy"
    dry_run_default: bool = True

    # Tools
    exiftool_path: str = "exiftool"

    # Business rules (subset for now)
    cluster_time_window_minutes: int = 45
    same_place_radius_m: int = 300
    place_change_radius_m: int = 1000
    stay_radius_km: int = 25
    max_gap_days_in_stay: int = 1

    event_min_items: int = 20
    month_min_items: int = 15
    quarter_fallback_enabled: bool = True

    live_subfolder_suffix: str = " - live"
    orphan_live_policy: str = "live_subfolder"  # live_subfolder | video_single | review | ignore

    hash_algorithm: str = "md5"
    duplicate_policy: str = "quarantine"

    geocode_cache: str = "./cache/geocode.json"
    geocode_provider: str = "nominatim"

    usual_place_min_distinct_days: int = 10
    confidence_review_enabled: bool = False

    # Set via .env, not config.yaml
    home_lat: float | None = None
    home_lon: float | None = None
    home_radius_km: float = 50.0
    away_event_min_items: int = 10

    log_level: str = "INFO"


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_config(*, config_path: Path, env_path: Path | None) -> AppConfig:
    config_path = config_path.expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        yaml_cfg = yaml.safe_load(f) or {}

    env: dict[str, str] = {}
    if env_path is not None:
        env_path = env_path.expanduser().resolve()
        if not env_path.exists():
            raise FileNotFoundError(f".env not found: {env_path}")
        env = {k: v for k, v in dotenv_values(env_path).items() if v is not None}

    # Required paths from env
    def req(name: str) -> str:
        v = env.get(name)
        if not v:
            raise ValueError(f"Missing required env var: {name}")
        return v

    source_root = Path(req("SOURCE_ROOT"))
    target_root = Path(req("TARGET_ROOT"))
    output_root = Path(env.get("OUTPUT_ROOT", str(target_root / "reports")))
    quarantine_root = Path(env.get("QUARANTINE_ROOT", str(target_root.parent / "_quarantine")))
    review_root = Path(env.get("REVIEW_ROOT", str(target_root.parent / "_A_REVOIR")))

    move_mode: MoveMode = env.get("MOVE_MODE", "copy").strip().lower()  # type: ignore[assignment]
    if move_mode not in {"copy", "move"}:
        raise ValueError("MOVE_MODE must be 'copy' or 'move'")

    dry_run_default = _parse_bool(env.get("DRY_RUN_DEFAULT"), True)

    def _opt_float(name: str) -> float | None:
        v = env.get(name)
        return float(v) if v is not None else None

    home_lat = _opt_float("HOME_LAT")
    home_lon = _opt_float("HOME_LON")
    home_radius_km = float(env.get("HOME_RADIUS_KM", "50"))
    away_event_min_items = int(env.get("AWAY_EVENT_MIN_ITEMS", "10"))

    cfg = AppConfig(
        paths=PathsConfig(
            source_root=source_root,
            target_root=target_root,
            output_root=output_root,
            quarantine_root=quarantine_root,
            review_root=review_root,
        ),
        move_mode=move_mode,
        dry_run_default=dry_run_default,
        home_lat=home_lat,
        home_lon=home_lon,
        home_radius_km=home_radius_km,
        away_event_min_items=away_event_min_items,
        **yaml_cfg,
    )

    return cfg


def ensure_dirs(cfg: AppConfig) -> None:
    cfg.paths.output_root.mkdir(parents=True, exist_ok=True)
    cfg.paths.quarantine_root.mkdir(parents=True, exist_ok=True)
    cfg.paths.review_root.mkdir(parents=True, exist_ok=True)


def validate_paths(cfg: AppConfig) -> None:
    src = cfg.paths.source_root
    tgt = cfg.paths.target_root

    if not src.exists():
        raise FileNotFoundError(f"SOURCE_ROOT does not exist: {src}")

    # Avoid scanning into the target tree
    try:
        src_resolved = src.expanduser().resolve()
        tgt_resolved = tgt.expanduser().resolve()
        if tgt_resolved == src_resolved or tgt_resolved.is_relative_to(src_resolved):
            raise ValueError("TARGET_ROOT must not be inside SOURCE_ROOT")
    except Exception:
        # On some mounts resolve/is_relative_to can be unreliable; keep a conservative check.
        pass
