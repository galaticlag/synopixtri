"""Multi-day stay fusion (spec §9): merge consecutive day clusters into a stay.

Simplification: continuity is judged on each day-group's GPS centroid only
(no historical "usual zones" persistence across runs - see geo.py docstring).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from . import geo
from .clustering import DayCluster


@dataclass
class MultiDayStay:
    stay_id: str
    day_clusters: list[DayCluster]

    @property
    def start_day(self):
        return self.day_clusters[0].day

    @property
    def end_day(self):
        return self.day_clusters[-1].day

    @property
    def duration_days(self) -> int:
        return (self.end_day - self.start_day).days + 1

    @property
    def media_count(self) -> int:
        return sum(c.media_count for c in self.day_clusters)

    @property
    def live_count(self) -> int:
        return sum(c.live_count for c in self.day_clusters)

    def centroid_gps(self) -> tuple[float, float] | None:
        points = [u.primary.gps for c in self.day_clusters for u in c.units if u.primary.gps is not None]
        return geo.centroid(points)


def _group_centroid(day_clusters: list[DayCluster], idxs: list[int]) -> tuple[float, float] | None:
    points = [
        u.primary.gps
        for i in idxs
        for u in day_clusters[i].units
        if u.primary.gps is not None
    ]
    return geo.centroid(points)


def build_multi_day_stays(
    day_clusters: list[DayCluster],
    *,
    stay_radius_km: float,
    max_gap_days: int,
    usual_zones: set[tuple[float, float]] | None,
) -> tuple[list[MultiDayStay], set[int]]:
    """Group consecutive day clusters into stays per spec §9.2.

    Returns (stays, consumed_indices) where consumed_indices are the indices
    into `day_clusters` that ended up inside a stay (spanning >= 2 days).
    """
    idx_by_day: dict = defaultdict(list)
    for i, c in enumerate(day_clusters):
        idx_by_day[c.day].append(i)
    days_sorted = sorted(idx_by_day.keys())
    usual = usual_zones or set()
    radius_m = stay_radius_km * 1000.0

    stays: list[MultiDayStay] = []
    consumed: set[int] = set()
    i = 0
    n = len(days_sorted)
    while i < n:
        group_days = [days_sorted[i]]
        group_idxs = list(idx_by_day[days_sorted[i]])
        centroid = _group_centroid(day_clusters, group_idxs)
        j = i + 1
        while j < n:
            gap_days = (days_sorted[j] - group_days[-1]).days - 1
            if gap_days > max_gap_days:
                break
            cand_idxs = idx_by_day[days_sorted[j]]
            cand_centroid = _group_centroid(day_clusters, cand_idxs)
            if centroid is None or cand_centroid is None:
                break
            if geo.zone_key(cand_centroid) in usual:
                break
            if geo.haversine_m(centroid, cand_centroid) > radius_m:
                break
            group_days.append(days_sorted[j])
            group_idxs.extend(cand_idxs)
            centroid = _group_centroid(day_clusters, group_idxs)
            j += 1
        if len(group_days) >= 2:
            stays.append(
                MultiDayStay(
                    stay_id=f"stay_{group_days[0].strftime('%Y%m%d')}",
                    day_clusters=[day_clusters[k] for k in sorted(group_idxs)],
                )
            )
            consumed.update(group_idxs)
        i = j

    return stays, consumed


def qualify_stay(*, duration_days: int, is_unusual_place: bool) -> str:
    """Provisional label per spec §9.3 (user renames afterwards)."""
    if is_unusual_place and duration_days >= 3:
        return "Vacances"
    if is_unusual_place:
        return "Sortie"
    if duration_days >= 3:
        return "Séjour"
    return "Evenement"
