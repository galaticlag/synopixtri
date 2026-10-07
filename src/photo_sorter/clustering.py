from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import geo
from .logical_units import LogicalUnit


@dataclass
class DayCluster:
    event_id: str
    day: date
    units: list[LogicalUnit]
    start_dt: datetime
    end_dt: datetime

    @property
    def media_count(self) -> int:
        return sum(1 for u in self.units if u.kind in {"photo_single", "video_single"})

    @property
    def live_count(self) -> int:
        return sum(1 for u in self.units if u.kind == "live_pair")

    @property
    def has_photos(self) -> bool:
        return any(u.kind in {"photo_single", "live_pair"} for u in self.units)

    @property
    def has_videos(self) -> bool:
        return any(u.kind == "video_single" for u in self.units)

    @property
    def month_key(self) -> str:
        return f"{self.day.year:04d}-{self.day.month:02d}"

    def centroid_gps(self) -> tuple[float, float] | None:
        points = [u.primary.gps for u in self.units if u.primary.gps is not None]
        return geo.centroid(points)


def _should_merge(
    prev_unit: LogicalUnit,
    curr_unit: LogicalUnit,
    *,
    same_day: bool,
    gap: timedelta,
    window: timedelta,
    same_place_radius_m: float,
    place_change_radius_m: float,
    usual_zones: set[tuple[float, float]],
) -> bool:
    """Fusion/separation rules within a day (spec §8.3)."""
    if not same_day:
        return False

    prev_gps = prev_unit.primary.gps
    curr_gps = curr_unit.primary.gps

    if prev_gps is not None and curr_gps is not None:
        dist = geo.haversine_m(prev_gps, curr_gps)
        if dist > place_change_radius_m:
            return False  # significant zone change -> new event
        if usual_zones and geo.zone_key(curr_gps) in usual_zones and geo.zone_key(prev_gps) not in usual_zones:
            return False  # return to a usual/home zone -> end of the event
        if gap <= window:
            return True
        return dist <= same_place_radius_m  # tolerance: same zone despite the time gap

    return gap <= window  # no GPS on one/both sides: fall back to pure temporal rule


def cluster_units_by_day(
    units: list[LogicalUnit],
    *,
    window_minutes: int,
    same_place_radius_m: float = 300.0,
    place_change_radius_m: float = 1000.0,
    usual_zones: set[tuple[float, float]] | None = None,
) -> list[DayCluster]:
    """Group logical units into per-day clusters using a time-gap window plus
    GPS-aware fusion/separation tolerances (spec §8.3).
    """
    ordered = sorted(units, key=lambda u: u.reference_datetime)
    clusters: list[DayCluster] = []
    window = timedelta(minutes=window_minutes)
    usual = usual_zones or set()

    current_units: list[LogicalUnit] = []
    current_day: date | None = None
    last_unit: LogicalUnit | None = None
    seq_per_day: dict[date, int] = {}

    def _flush() -> None:
        if not current_units:
            return
        assert current_day is not None
        seq = seq_per_day.get(current_day, 0) + 1
        seq_per_day[current_day] = seq
        clusters.append(
            DayCluster(
                event_id=f"evt_{current_day.strftime('%Y%m%d')}_{seq:02d}",
                day=current_day,
                units=list(current_units),
                start_dt=current_units[0].reference_datetime,
                end_dt=current_units[-1].reference_datetime,
            )
        )

    for unit in ordered:
        dt = unit.reference_datetime
        day = dt.date()
        if current_day is None:
            current_day = day
            current_units = [unit]
            last_unit = unit
            continue

        same_day = day == current_day
        gap = dt - last_unit.reference_datetime if last_unit is not None else timedelta(0)
        merge = last_unit is not None and _should_merge(
            last_unit,
            unit,
            same_day=same_day,
            gap=gap,
            window=window,
            same_place_radius_m=same_place_radius_m,
            place_change_radius_m=place_change_radius_m,
            usual_zones=usual,
        )
        if merge:
            current_units.append(unit)
        else:
            _flush()
            current_day = day
            current_units = [unit]
        last_unit = unit

    _flush()
    return clusters


def apply_daily_estimation(day_clusters: list[DayCluster]) -> list[DayCluster]:
    """Backfill missing GPS on units when their day has a single cluster and at
    least one unit in it has GPS (spec §7.1.3, "estimation journalière prudente").
    Only fills placement metadata; does not re-run cluster splitting.
    """
    idxs_by_day: dict[date, list[int]] = {}
    for i, c in enumerate(day_clusters):
        idxs_by_day.setdefault(c.day, []).append(i)

    updated = list(day_clusters)
    for day, idxs in idxs_by_day.items():
        if len(idxs) != 1:
            continue
        idx = idxs[0]
        cluster = updated[idx]
        points = [u.primary.gps for u in cluster.units if u.primary.gps is not None]
        dominant = geo.dominant_zone(points)
        if dominant is None:
            continue

        new_units = []
        changed = False
        for u in cluster.units:
            if u.primary.gps is not None:
                new_units.append(u)
                continue
            changed = True
            new_primary = dataclasses.replace(u.primary, gps=dominant)
            new_live = dataclasses.replace(u.live, gps=dominant) if u.live is not None else None
            new_units.append(dataclasses.replace(u, primary=new_primary, live=new_live))
        if changed:
            updated[idx] = dataclasses.replace(cluster, units=new_units)

    return updated
