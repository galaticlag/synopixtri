from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import geo
from .clustering import apply_daily_estimation, cluster_units_by_day
from .destination_engine import VolumetricOptions, decide_destinations, folder_name_for_month
from .logical_units import LogicalUnit, MediaRecord, build_logical_units
from .plan_writer import PlanItem
from .state_db import StateStore


DATE_KEYS = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "File:FileModifyDate",
    "File:FileCreateDate",
)

_MONTHLY_FOLDER_RE = re.compile(r"^\d{4}\.\d{2} Vie de famille$")
_QUARTERLY_FOLDER_RE = re.compile(r"^\d{4}\.Q[1-4] Vie de famille$")

_ROUTINE_REASONS = {
    "monthly_routine_bucket",
    "quarterly_routine_bucket",
    "monthly_routine_bucket_fallback",
}


@dataclass(frozen=True)
class BuildPlanOptions:
    mode: str
    duplicate_policy: str
    orphan_live_policy: str = "review"  # review | video_single | ignore
    cluster_time_window_minutes: int = 45
    event_min_items: int = 20
    month_min_items: int = 15
    quarter_fallback_enabled: bool = True
    live_subfolder_suffix: str = " - live"
    same_place_radius_m: float = 300.0
    place_change_radius_m: float = 1000.0
    stay_radius_km: float = 25.0
    max_gap_days_in_stay: int = 1
    usual_place_min_distinct_days: int = 10
    confidence_review_enabled: bool = False
    geocode_cache_path: str = "./cache/geocode.json"
    home_gps: tuple[float, float] | None = None
    home_radius_km: float = 50.0
    away_event_min_items: int = 10


def _parse_exif_datetime(raw: str) -> datetime | None:
    # ExifTool often returns values like "2026:07:25 16:10:45+02:00".
    text = raw.strip()
    if not text:
        return None
    text = text.replace("T", " ")
    if "+" in text:
        text = text.split("+", 1)[0].strip()
    if "-" in text[10:]:
        text = text.split("-", 1)[0].strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def select_reference_datetime(path: Path, exif: dict[str, Any]) -> tuple[datetime, bool]:
    """Returns (reference_datetime, metadata_ok). metadata_ok is False when we
    had to fall back to the file's mtime (weaker signal, used by confidence scoring).
    """
    for key in DATE_KEYS:
        value = exif.get(key)
        if isinstance(value, str):
            dt = _parse_exif_datetime(value)
            if dt is not None:
                return dt, True
    return datetime.fromtimestamp(path.stat().st_mtime), False


def month_key_for_datetime(ref_dt: datetime) -> str:
    return f"{ref_dt.year:04d}-{ref_dt.month:02d}"


def month_folder_for_key(month_key: str) -> str:
    year, month = month_key.split("-", 1)
    return f"{year}/{year}.{month} Vie de famille"


def classify_or_defer(
    *,
    source: Path,
    sha: str,
    size: int,
    exif: dict[str, Any],
    options: BuildPlanOptions,
    state: StateStore,
    seen_in_run: set[str],
) -> PlanItem | MediaRecord:
    """Resolve already-placed files and in-run duplicates immediately.

    Returns a finished PlanItem for skip/duplicate cases, or a MediaRecord for
    files that still need Live pairing + clustering before a destination can
    be decided.
    """
    ref_dt, metadata_ok = select_reference_datetime(source, exif)
    bucket_key = month_key_for_datetime(ref_dt)

    existing = state.get_placement_by_sha(sha)
    if existing is not None:
        return PlanItem(
            source=str(source),
            sha=sha,
            size=size,
            exif=exif,
            kind="file",
            reference_datetime=ref_dt.isoformat(sep=" "),
            action="skip",
            destination=existing,
            reason="already_processed_sha",
            bucket_key=bucket_key,
        )

    if sha in seen_in_run:
        if options.duplicate_policy == "quarantine":
            quarantine_target = str(Path("DUPLICATES") / f"{sha[:12]}_{source.name}")
            return PlanItem(
                source=str(source),
                sha=sha,
                size=size,
                exif=exif,
                kind="duplicate_exact",
                reference_datetime=ref_dt.isoformat(sep=" "),
                action="quarantine",
                destination=quarantine_target,
                reason="duplicate_sha_in_current_run",
                bucket_key=bucket_key,
            )
        if options.duplicate_policy == "report_only":
            return PlanItem(
                source=str(source),
                sha=sha,
                size=size,
                exif=exif,
                kind="duplicate_exact",
                reference_datetime=ref_dt.isoformat(sep=" "),
                action="review",
                destination=None,
                reason="duplicate_sha_report_only",
                bucket_key=bucket_key,
            )
        return PlanItem(
            source=str(source),
            sha=sha,
            size=size,
            exif=exif,
            kind="duplicate_exact",
            reference_datetime=ref_dt.isoformat(sep=" "),
            action="skip",
            destination=None,
            reason="duplicate_sha_skip",
            bucket_key=bucket_key,
        )

    seen_in_run.add(sha)
    gps = geo.extract_gps(exif)
    return MediaRecord(
        path=source,
        sha=sha,
        size=size,
        exif=exif,
        reference_datetime=ref_dt,
        gps=gps,
        metadata_ok=metadata_ok,
    )


def _apply_orphan_policy(
    units: list[LogicalUnit], *, policy: str
) -> tuple[list[LogicalUnit], list[PlanItem]]:
    clusterable: list[LogicalUnit] = []
    direct_items: list[PlanItem] = []

    for unit in units:
        if unit.kind != "orphan_live_mov":
            clusterable.append(unit)
            continue

        record = unit.primary
        bucket_key = month_key_for_datetime(unit.reference_datetime)

        if policy == "live_subfolder":
            # Route to the monthly live subfolder instead of the main folder.
            clusterable.append(
                LogicalUnit(
                    kind="orphan_live_single",
                    primary=record,
                    live=None,
                    reference_datetime=unit.reference_datetime,
                )
            )
        elif policy == "video_single":
            clusterable.append(
                LogicalUnit(
                    kind="video_single",
                    primary=record,
                    live=None,
                    reference_datetime=unit.reference_datetime,
                )
            )
        elif policy == "ignore":
            direct_items.append(
                PlanItem(
                    source=str(record.path),
                    sha=record.sha,
                    size=record.size,
                    exif=record.exif,
                    kind="orphan_live_mov",
                    reference_datetime=unit.reference_datetime.isoformat(sep=" "),
                    action="skip",
                    destination=None,
                    reason="orphan_live_ignored",
                    bucket_key=bucket_key,
                )
            )
        else:  # "review" (default, conservative per spec §5.2)
            direct_items.append(
                PlanItem(
                    source=str(record.path),
                    sha=record.sha,
                    size=record.size,
                    exif=record.exif,
                    kind="orphan_live_mov",
                    reference_datetime=unit.reference_datetime.isoformat(sep=" "),
                    action="review",
                    destination=str(Path("A_REVOIR") / "live" / record.path.name),
                    reason="orphan_live_review",
                    bucket_key=bucket_key,
                )
            )

    return clusterable, direct_items


def build_plan_items_from_records(
    records: list[MediaRecord], *, options: BuildPlanOptions
) -> list[PlanItem]:
    """Live-pair, cluster and assign destinations for files not yet placed."""
    if not records:
        return []

    units = build_logical_units(records)
    clusterable_units, direct_items = _apply_orphan_policy(units, policy=options.orphan_live_policy)

    # GPS enrichment (spec §7): inherit GPS from nearby units, then learn which
    # zones are "usual" (home/routine) from the current batch (see geo.py for
    # the documented simplification vs full cross-run historical learning).
    enriched_units = geo.inherit_gps(clusterable_units)
    usual_zones = geo.detect_usual_zones(
        enriched_units, min_distinct_days=options.usual_place_min_distinct_days
    )

    day_clusters = cluster_units_by_day(
        enriched_units,
        window_minutes=options.cluster_time_window_minutes,
        same_place_radius_m=options.same_place_radius_m,
        place_change_radius_m=options.place_change_radius_m,
        usual_zones=usual_zones,
    )
    day_clusters = apply_daily_estimation(day_clusters)

    geocoder = geo.GeocodeCache(Path(options.geocode_cache_path))
    placements = decide_destinations(
        day_clusters,
        options=VolumetricOptions(
            event_min_items=options.event_min_items,
            month_min_items=options.month_min_items,
            quarter_fallback_enabled=options.quarter_fallback_enabled,
            live_subfolder_suffix=options.live_subfolder_suffix,
            stay_radius_km=options.stay_radius_km,
            max_gap_days_in_stay=options.max_gap_days_in_stay,
            confidence_review_enabled=options.confidence_review_enabled,
            home_gps=options.home_gps,
            home_radius_km=options.home_radius_km,
            away_event_min_items=options.away_event_min_items,
        ),
        geocoder=geocoder,
        usual_zones=usual_zones,
    )

    items: list[PlanItem] = list(direct_items)
    for placement in placements:
        unit = placement.unit
        bucket_key = month_key_for_datetime(unit.reference_datetime)
        primary = unit.primary

        primary_kind = "live_pair_photo" if unit.kind == "live_pair" else unit.kind
        if placement.force_review:
            primary_action = "review"
            primary_destination = str(Path("A_REVOIR") / "low_confidence" / primary.path.name)
            primary_reason = f"{placement.reason}_low_confidence_review"
        else:
            primary_action = options.mode
            primary_destination = placement.primary_destination
            primary_reason = placement.reason

        items.append(
            PlanItem(
                source=str(primary.path),
                sha=primary.sha,
                size=primary.size,
                exif=primary.exif,
                kind=primary_kind,
                reference_datetime=unit.reference_datetime.isoformat(sep=" "),
                action=primary_action,
                destination=primary_destination,
                reason=primary_reason,
                bucket_key=bucket_key,
                confidence=placement.confidence,
            )
        )

        if unit.kind == "live_pair":
            assert unit.live is not None
            assert placement.live_destination is not None
            if placement.force_review:
                live_action = "review"
                live_destination = str(Path("A_REVOIR") / "low_confidence" / unit.live.path.name)
                live_reason = f"{placement.reason}_live_subfolder_low_confidence_review"
            else:
                live_action = options.mode
                live_destination = placement.live_destination
                live_reason = f"{placement.reason}_live_subfolder"

            items.append(
                PlanItem(
                    source=str(unit.live.path),
                    sha=unit.live.sha,
                    size=unit.live.size,
                    exif=unit.live.exif,
                    kind="live_pair_live",
                    reference_datetime=unit.reference_datetime.isoformat(sep=" "),
                    action=live_action,
                    destination=live_destination,
                    reason=live_reason,
                    bucket_key=bucket_key,
                    confidence=placement.confidence,
                )
            )

    return items


def _routine_folder_choice_for_month(new_items: list[PlanItem], month_key: str) -> str | None:
    """Folder name (e.g. "2026.03 Vie de famille") chosen by THIS run's newly
    placed routine items for a given month, if any. Only non-live items are
    considered so the returned name is the plain folder (not the "- live"
    subfolder variant).
    """
    for item in new_items:
        if item.bucket_key != month_key:
            continue
        if item.reason not in _ROUTINE_REASONS:
            continue
        if not item.destination:
            continue
        return Path(item.destination).parent.name
    return None


def build_reclassification_items(
    *,
    state: StateStore,
    target_root: Path,
    impacted_month_keys: set[str],
    new_items: list[PlanItem] | None = None,
    live_subfolder_suffix: str = " - live",
) -> list[PlanItem]:
    """Move already-placed routine files back in line with this run's monthly
    vs quarterly bucket choice for impacted months (spec §10.3 threshold is
    re-evaluated whenever new files land in a month that already has placed
    files, so a month's files never end up split across both a monthly and a
    quarterly folder).

    For each impacted month, the authoritative folder name is whatever this
    run's own new routine items were placed into (if any); historical rows
    already sitting in a *different* monthly/quarterly bucket are moved to
    match. If this run placed no new routine item for that month, we fall
    back to the naive plain-monthly expectation.

    Limitation: files already placed in an event- or stay-dedicated folder
    (§9/§10.3 pass 1) are left untouched, since re-deciding those requires
    re-running full clustering over the month (not implemented here) -
    otherwise this could incorrectly force them back into a routine folder.
    """
    new_items = new_items or []
    items: list[PlanItem] = []
    for row in state.get_placements_for_months(impacted_month_keys):
        month_key = row["month_key"]
        source_name = row["source_name"]
        if month_key is None or source_name is None:
            continue
        month_key = str(month_key)

        current_rel = str(row["destination_rel"])
        current_parent_name = Path(current_rel).parent.name

        is_live = current_parent_name.endswith(live_subfolder_suffix)
        base_current_name = (
            current_parent_name[: -len(live_subfolder_suffix)] if is_live else current_parent_name
        )
        if not _MONTHLY_FOLDER_RE.match(base_current_name) and not _QUARTERLY_FOLDER_RE.match(base_current_name):
            continue  # event/stay dedicated folder: not re-evaluated here.

        target_folder_name = _routine_folder_choice_for_month(new_items, month_key)
        if target_folder_name is None:
            target_folder_name = folder_name_for_month(month_key)

        if base_current_name == target_folder_name:
            continue

        year = month_key.split("-", 1)[0]
        if is_live:
            expected_rel = str(
                Path(year) / target_folder_name / f"{target_folder_name}{live_subfolder_suffix}" / source_name
            )
        else:
            expected_rel = str(Path(year) / target_folder_name / source_name)

        if current_rel == expected_rel:
            continue

        current_abs = target_root / current_rel
        if not current_abs.exists() or not current_abs.is_file():
            continue

        items.append(
            PlanItem(
                source=str(current_abs),
                sha=str(row["sha"]),
                size=current_abs.stat().st_size,
                exif={},
                kind="reclassify",
                reference_datetime=None,
                action="move",
                destination=expected_rel,
                reason="reclassify_impacted_month",
                bucket_key=month_key,
            )
        )

    return items
