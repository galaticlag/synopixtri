"""GPS extraction, zone bucketing and reverse geocoding (spec §7).

Reverse geocoding uses the public Nominatim API (OpenStreetMap), as required by
spec.md §11.3 / §14. It is rate-limited to one request per second and cached
locally in a JSON file so repeated runs never re-query the same coordinates.
Network failures degrade gracefully to "Lieu inconnu" (never blocks planning).
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

UNKNOWN_PLACE = "Lieu inconnu"

_GPS_LAT_KEYS = ("EXIF:GPSLatitude", "Composite:GPSLatitude", "QuickTime:GPSLatitude")
_GPS_LON_KEYS = ("EXIF:GPSLongitude", "Composite:GPSLongitude", "QuickTime:GPSLongitude")
_GPS_LAT_REF_KEYS = ("EXIF:GPSLatitudeRef", "QuickTime:GPSLatitudeRef")
_GPS_LON_REF_KEYS = ("EXIF:GPSLongitudeRef", "QuickTime:GPSLongitudeRef")


def _first(exif: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in exif and exif[key] not in (None, ""):
            return exif[key]
    return None


def extract_gps(exif: dict[str, Any]) -> tuple[float, float] | None:
    """Extract signed (lat, lon) decimal degrees from ExifTool JSON output.

    Handles the plain EXIF/QuickTime numeric fields (exiftool run with -n) plus
    the QuickTime:GPSCoordinates "lat, lon, alt" string some iPhone videos use.
    """
    coords = exif.get("QuickTime:GPSCoordinates")
    if isinstance(coords, str) and coords.strip():
        parts = [p.strip() for p in coords.split(",")]
        if len(parts) >= 2:
            try:
                return (float(parts[0]), float(parts[1]))
            except ValueError:
                pass

    lat_raw = _first(exif, _GPS_LAT_KEYS)
    lon_raw = _first(exif, _GPS_LON_KEYS)
    if lat_raw is None or lon_raw is None:
        return None
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        return None

    lat_ref = _first(exif, _GPS_LAT_REF_KEYS)
    lon_ref = _first(exif, _GPS_LON_REF_KEYS)
    if isinstance(lat_ref, str) and lat_ref.strip().upper().startswith("S"):
        lat = -abs(lat)
    if isinstance(lon_ref, str) and lon_ref.strip().upper().startswith("W"):
        lon = -abs(lon)
    return (lat, lon)


def haversine_m(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points, in meters."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    r = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def zone_key(gps: tuple[float, float], *, precision: int = 3) -> tuple[float, float]:
    """Round coordinates to a coarse grid (~111m at 3 decimals) for zone bucketing.

    This is a pragmatic approximation of "zones de vie" (spec §7.2): a real
    implementation would use geohash/DBSCAN over historical placements, which
    isn't persisted yet. Here zones are only learned from the current batch.
    """
    return (round(gps[0], precision), round(gps[1], precision))


def centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    return (lat, lon)


def dominant_zone(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Centroid of the points falling in the most frequently occurring zone bucket."""
    if not points:
        return None
    by_zone: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for p in points:
        by_zone.setdefault(zone_key(p), []).append(p)
    best_zone = max(by_zone, key=lambda z: len(by_zone[z]))
    return centroid(by_zone[best_zone])


def inherit_gps(units: list, *, window_minutes: int = 20) -> list:
    """Fill missing GPS on units by borrowing it from the nearest-in-time unit
    that has GPS, within a window (spec §7.1.2: video without GPS borrows from
    the closest photo in time, +-10-20 min). Approximated across the whole
    batch rather than strictly "same cluster" (clusters aren't known yet).
    """
    import dataclasses

    ordered = sorted(units, key=lambda u: u.reference_datetime)
    window_seconds = window_minutes * 60
    result = []
    for unit in ordered:
        if unit.primary.gps is not None:
            result.append(unit)
            continue
        best_gps = None
        best_delta = None
        for other in ordered:
            other_gps = other.primary.gps
            if other_gps is None:
                continue
            delta = abs((other.reference_datetime - unit.reference_datetime).total_seconds())
            if delta <= window_seconds and (best_delta is None or delta < best_delta):
                best_gps = other_gps
                best_delta = delta
        if best_gps is None:
            result.append(unit)
            continue
        new_primary = dataclasses.replace(unit.primary, gps=best_gps)
        new_live = dataclasses.replace(unit.live, gps=best_gps) if unit.live is not None else None
        result.append(dataclasses.replace(unit, primary=new_primary, live=new_live))
    return result


def detect_usual_zones(units: list, *, min_distinct_days: int) -> set[tuple[float, float]]:
    """A zone is "usual" if it appears across at least N distinct days in the
    current batch (spec §7.2 simplified: no cross-run historical learning yet).
    """
    days_by_zone: dict[tuple[float, float], set] = {}
    for unit in units:
        gps = unit.primary.gps
        if gps is None:
            continue
        zone = zone_key(gps)
        days_by_zone.setdefault(zone, set()).add(unit.reference_datetime.date())
    return {zone for zone, days in days_by_zone.items() if len(days) >= min_distinct_days}


def is_usual_place(gps: tuple[float, float] | None, usual_zones: set[tuple[float, float]]) -> bool:
    return gps is not None and zone_key(gps) in usual_zones


def is_near_home(
    gps: tuple[float, float] | None,
    home_gps: tuple[float, float] | None,
    home_radius_km: float,
) -> bool:
    if gps is None or home_gps is None:
        return False
    return haversine_m(gps, home_gps) <= home_radius_km * 1000



class GeocodeCache:
    """Local JSON cache + rate-limited Nominatim reverse geocoding client."""

    def __init__(self, cache_path: Path, *, user_agent: str = "photo-sorter/0.1 (local-tool)"):
        self.cache_path = cache_path
        self.user_agent = user_agent
        self._data: dict[str, str] = {}
        self._last_request = 0.0
        if cache_path.exists():
            try:
                self._data = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def label_for(self, gps: tuple[float, float] | None) -> str:
        if gps is None:
            return UNKNOWN_PLACE
        key = f"{round(gps[0], 3)},{round(gps[1], 3)}"
        if key in self._data:
            return self._data[key]

        label = self._reverse_geocode(gps)
        self._data[key] = label
        self._save()
        return label

    def _reverse_geocode(self, gps: tuple[float, float]) -> str:
        elapsed = time.monotonic() - self._last_request
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self._last_request = time.monotonic()

        url = (
            "https://nominatim.openstreetmap.org/reverse"
            f"?format=jsonv2&lat={gps[0]:.6f}&lon={gps[1]:.6f}&zoom=10&accept-language=fr"
        )
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return UNKNOWN_PLACE

        address = payload.get("address", {}) if isinstance(payload, dict) else {}
        for field in ("city", "town", "village", "municipality", "county", "state"):
            value = address.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return UNKNOWN_PLACE
