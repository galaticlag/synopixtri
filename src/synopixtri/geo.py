"""Distances and reverse geocoding (Nominatim, cached, rate limited)."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

_USER_AGENT = "synopixtri/0.1 (self-hosted photo sorter)"
_lock = threading.Lock()
_last_call = 0.0


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def _city(address: dict) -> str | None:
    for key in ("city", "town", "village", "municipality", "hamlet", "county"):
        if address.get(key):
            return str(address[key])
    return None


def reverse_geocode(
    conn: sqlite3.Connection, lat: float, lon: float, language: str = "fr", enabled: bool = True
) -> str | None:
    """City name for a coordinate, or None. Results are cached by ~100 m cell."""
    global _last_call
    key = f"{lat:.3f},{lon:.3f},{language}"
    row = conn.execute("SELECT city FROM geocode_cache WHERE key=?", (key,)).fetchone()
    if row:
        return row["city"]
    if not enabled:
        return None
    query = urllib.parse.urlencode(
        {"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "zoom": 12, "accept-language": language}
    )
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/reverse?{query}", headers={"User-Agent": _USER_AGENT}
    )
    with _lock:  # Nominatim usage policy: at most one request per second.
        wait = 1.1 - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        finally:
            _last_call = time.monotonic()
    city = _city(data.get("address", {}))
    if city:
        conn.execute("INSERT OR REPLACE INTO geocode_cache(key, city) VALUES(?, ?)", (key, city))
        conn.commit()
    return city
