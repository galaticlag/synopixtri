from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import confidence, geo
from .clustering import DayCluster
from .logical_units import LogicalUnit
from .stay import MultiDayStay, build_multi_day_stays, qualify_stay


def format_date_range(start: date, end: date) -> str:
    """Date-range compression per spec §11.4."""
    if start == end:
        return f"{start.year:04d}.{start.month:02d}.{start.day:02d}"
    if start.year == end.year and start.month == end.month:
        return f"{start.year:04d}.{start.month:02d}.{start.day:02d}~{end.day:02d}"
    if start.year == end.year:
        return f"{start.year:04d}.{start.month:02d}.{start.day:02d}~{end.month:02d}.{end.day:02d}"
    return f"{start.year:04d}.{start.month:02d}.{start.day:02d}~{end.year:04d}.{end.month:02d}.{end.day:02d}"


def folder_name_for_event(day: date, *, place: str, label: str = "Evenement") -> str:
    return f"{day.year:04d}.{day.month:02d}.{day.day:02d} {label}, {place}"


def folder_name_for_stay(start: date, end: date, *, place: str, label: str) -> str:
    return f"{format_date_range(start, end)} {label}, {place}"


def folder_name_for_month(month_key: str) -> str:
    year, month = month_key.split("-", 1)
    return f"{year}.{month} Vie de famille"


def folder_name_for_quarter(month_key: str) -> str:
    year, month = month_key.split("-", 1)
    quarter = (int(month) - 1) // 3 + 1
    return f"{year}.Q{quarter} Vie de famille"


def folder_rel_path(year: str, folder_name: str) -> Path:
    return Path(year) / folder_name


def live_subfolder_rel_path(year: str, folder_name: str, suffix: str) -> Path:
    return Path(year) / folder_name / f"{folder_name}{suffix}"


@dataclass(frozen=True)
class VolumetricOptions:
    event_min_items: int
    month_min_items: int
    quarter_fallback_enabled: bool
    live_subfolder_suffix: str
    stay_radius_km: float = 25.0
    max_gap_days_in_stay: int = 1
    confidence_review_enabled: bool = False
    home_gps: tuple[float, float] | None = None
    home_radius_km: float = 50.0
    away_event_min_items: int = 10


@dataclass(frozen=True)
class UnitPlacement:
    unit: LogicalUnit
    primary_destination: str
    live_destination: str | None
    reason: str
    confidence: float | None = None
    force_review: bool = False


def _emit(
    units: list[LogicalUnit],
    *,
    folder_rel: Path,
    live_rel: Path,
    reason: str,
    conf: float | None,
    force_review: bool,
) -> list[UnitPlacement]:
    placements: list[UnitPlacement] = []
    for unit in units:
        if unit.kind == "live_pair":
            assert unit.live is not None
            placements.append(
                UnitPlacement(
                    unit=unit,
                    primary_destination=str(folder_rel / unit.primary.path.name),
                    live_destination=str(live_rel / unit.live.path.name),
                    reason=reason,
                    confidence=conf,
                    force_review=force_review,
                )
            )
        elif unit.kind == "orphan_live_single":
            # Orphan live MOV without paired JPG — place directly in live subfolder.
            placements.append(
                UnitPlacement(
                    unit=unit,
                    primary_destination=str(live_rel / unit.primary.path.name),
                    live_destination=None,
                    reason=reason,
                    confidence=conf,
                    force_review=force_review,
                )
            )
        else:
            placements.append(
                UnitPlacement(
                    unit=unit,
                    primary_destination=str(folder_rel / unit.primary.path.name),
                    live_destination=None,
                    reason=reason,
                    confidence=conf,
                    force_review=force_review,
                )
            )
    return placements


def decide_destinations(
    day_clusters: list[DayCluster],
    *,
    options: VolumetricOptions,
    geocoder: geo.GeocodeCache | None = None,
    usual_zones: set[tuple[float, float]] | None = None,
) -> list[UnitPlacement]:
    """Two-pass volumetric decision (spec §10.3) with multi-day stay fusion
    (spec §9) and place-aware folder naming (spec §11).
    """
    usual = usual_zones or set()

    def _place_for(gps: tuple[float, float] | None) -> str:
        if geocoder is None:
            return geo.UNKNOWN_PLACE
        return geocoder.label_for(gps)

    stays, consumed_idx = build_multi_day_stays(
        day_clusters,
        stay_radius_km=options.stay_radius_km,
        max_gap_days=options.max_gap_days_in_stay,
        usual_zones=usual,
    )

    extracted_stay_cluster_ids: set[int] = set()
    placements: list[UnitPlacement] = []

    def _is_unusual(centroid: tuple[float, float] | None) -> bool:
        if geo.is_near_home(centroid, options.home_gps, options.home_radius_km):
            return False
        return not geo.is_usual_place(centroid, usual)

    def _event_threshold(centroid: tuple[float, float] | None) -> int:
        # Use a lower threshold for photos clearly far from home.
        if options.home_gps is not None and not geo.is_near_home(centroid, options.home_gps, options.home_radius_km):
            return options.away_event_min_items
        return options.event_min_items

    # Passe 1a: extract strong multi-day stays.
    for stay in stays:
        centroid = stay.centroid_gps()
        if stay.media_count < _event_threshold(centroid):
            continue
        extracted_stay_cluster_ids.update(id(c) for c in stay.day_clusters)

        is_unusual = _is_unusual(centroid)
        label = qualify_stay(duration_days=stay.duration_days, is_unusual_place=is_unusual)
        place = _place_for(centroid)
        folder_name = folder_name_for_stay(stay.start_day, stay.end_day, place=place, label=label)

        year = f"{stay.start_day.year:04d}"
        folder_rel = folder_rel_path(year, folder_name)
        live_rel = live_subfolder_rel_path(year, folder_name, options.live_subfolder_suffix)

        has_photos = any(c.has_photos for c in stay.day_clusters)
        has_videos = any(c.has_videos for c in stay.day_clusters)
        conf = confidence.score_cluster(
            media_count=stay.media_count,
            is_usual_place=not is_unusual,
            duration_hours=None,
            has_photos=has_photos,
            has_videos=has_videos,
            start_dt=stay.day_clusters[0].start_dt,
            event_min_items=options.event_min_items,
            metadata_ok=all(u.primary.metadata_ok for c in stay.day_clusters for u in c.units),
        )
        force_review = options.confidence_review_enabled and conf < 3
        units = [u for c in stay.day_clusters for u in c.units]
        placements.extend(
            _emit(
                units,
                folder_rel=folder_rel,
                live_rel=live_rel,
                reason="event_stay_dedicated_folder",
                conf=conf,
                force_review=force_review,
            )
        )

    # Passe 1b: extract individually-strong single-day clusters (standalone,
    # or part of a stay that did not meet the threshold).
    extracted_day_idx: set[int] = set()
    for i, cluster in enumerate(day_clusters):
        if id(cluster) in extracted_stay_cluster_ids:
            continue
        centroid = cluster.centroid_gps()
        if cluster.media_count < _event_threshold(centroid):
            continue
        extracted_day_idx.add(i)

        is_unusual = _is_unusual(centroid)
        place = _place_for(centroid)
        folder_name = folder_name_for_event(cluster.day, place=place)

        year = f"{cluster.day.year:04d}"
        folder_rel = folder_rel_path(year, folder_name)
        live_rel = live_subfolder_rel_path(year, folder_name, options.live_subfolder_suffix)

        duration_hours = (cluster.end_dt - cluster.start_dt).total_seconds() / 3600
        conf = confidence.score_cluster(
            media_count=cluster.media_count,
            is_usual_place=not is_unusual,
            duration_hours=duration_hours,
            has_photos=cluster.has_photos,
            has_videos=cluster.has_videos,
            start_dt=cluster.start_dt,
            event_min_items=options.event_min_items,
            metadata_ok=all(u.primary.metadata_ok for u in cluster.units),
        )
        force_review = options.confidence_review_enabled and conf < 3
        placements.extend(
            _emit(
                cluster.units,
                folder_rel=folder_rel,
                live_rel=live_rel,
                reason="event_dedicated_folder",
                conf=conf,
                force_review=force_review,
            )
        )

    # Passe 2: aggregate remaining "routine" clusters per month, month vs quarter.
    routine_by_month: dict[str, list[int]] = {}
    for i, cluster in enumerate(day_clusters):
        if id(cluster) in extracted_stay_cluster_ids or i in extracted_day_idx:
            continue
        routine_by_month.setdefault(cluster.month_key, []).append(i)

    month_choice: dict[str, tuple[str, str]] = {}
    for month_key, idxs in routine_by_month.items():
        routine_count = sum(day_clusters[i].media_count for i in idxs)
        if routine_count >= options.month_min_items:
            month_choice[month_key] = (folder_name_for_month(month_key), "monthly_routine_bucket")
        elif options.quarter_fallback_enabled:
            month_choice[month_key] = (folder_name_for_quarter(month_key), "quarterly_routine_bucket")
        else:
            month_choice[month_key] = (folder_name_for_month(month_key), "monthly_routine_bucket_fallback")

    for month_key, idxs in routine_by_month.items():
        folder_name, reason = month_choice[month_key]
        for i in idxs:
            cluster = day_clusters[i]
            year = f"{cluster.day.year:04d}"
            folder_rel = folder_rel_path(year, folder_name)
            live_rel = live_subfolder_rel_path(year, folder_name, options.live_subfolder_suffix)
            placements.extend(
                _emit(
                    cluster.units,
                    folder_rel=folder_rel,
                    live_rel=live_rel,
                    reason=reason,
                    conf=None,
                    force_review=False,
                )
            )

    return placements

