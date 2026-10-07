"""Geographic zones: where media were taken decides how they are treated."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .geo import haversine_km

TYPES = {"home", "work", "frequent", "exclusion"}
ACTIONS = {"aside", "leave", "review"}  # what work / exclusion zones do with the media
LABELS = {"home": "Domicile", "work": "Travail", "frequent": "Lieu fréquent", "exclusion": "Exclusion"}


def _window(text: str, local_time) -> bool:
    start_s, end_s = text.split("-")
    start = datetime.strptime(start_s.strip(), "%H:%M").time()
    end = datetime.strptime(end_s.strip(), "%H:%M").time()
    if start <= end:
        return start <= local_time <= end
    return local_time >= start or local_time <= end


def validate(data: dict) -> dict:
    """Return a clean zone dict, or raise ValueError."""
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("a zone needs a name")
    kind = str(data.get("type", ""))
    if kind not in TYPES:
        raise ValueError(f"type must be one of {sorted(TYPES)}")
    shape = str(data.get("shape", "circle"))
    geometry = data.get("geometry") or {}
    if shape == "circle":
        try:
            lat, lon, radius = float(geometry["lat"]), float(geometry["lon"]), float(geometry["radius_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("a circle needs lat, lon and radius_m") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or radius <= 0:
            raise ValueError("circle out of range")
        geometry = {"lat": lat, "lon": lon, "radius_m": radius}
    elif shape == "polygon":
        try:
            points = [[float(a), float(b)] for a, b in geometry["points"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("a polygon needs a list of [lat, lon] points") from exc
        if len(points) < 3:
            raise ValueError("a polygon needs at least 3 points")
        geometry = {"points": points}
    else:
        raise ValueError("shape must be circle or polygon")
    action = str(data.get("action") or "aside")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")
    weekdays = data.get("weekdays")
    if weekdays in (None, [], ""):
        weekdays = None
    else:
        weekdays = sorted({int(d) for d in weekdays})
        if any(d < 0 or d > 6 for d in weekdays):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
    window = (data.get("time_window") or "").strip() or None
    if window:
        try:
            _window(window, datetime.min.time())
        except ValueError as exc:
            raise ValueError("time_window must look like HH:MM-HH:MM") from exc
    volume = data.get("volume_exception")
    volume = int(volume) if volume not in (None, "", 0) else None
    event_min = data.get("event_min")
    event_min = int(event_min) if event_min not in (None, "") else None
    if (volume is not None and volume < 1) or (event_min is not None and event_min < 0):
        raise ValueError("volume_exception must be >= 1 and event_min >= 0")
    return {
        "name": name, "type": kind, "shape": shape, "geometry": geometry, "action": action,
        "weekdays": weekdays, "time_window": window, "volume_exception": volume, "event_min": event_min,
        "place_name": (data.get("place_name") or "").strip() or None,
        "label": (data.get("label") or "").strip() or None,
        "enabled": bool(data.get("enabled", True)),
    }  # fmt: skip


def _row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "type": row["type"], "shape": row["shape"],
        "geometry": json.loads(row["geometry_json"]), "action": row["action"],
        "weekdays": json.loads(row["weekdays_json"]) if row["weekdays_json"] else None,
        "time_window": row["time_window"], "volume_exception": row["volume_exception"],
        "event_min": row["event_min"], "place_name": row["place_name"], "label": row["label"],
        "enabled": bool(row["enabled"]),
    }  # fmt: skip


def load(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM zone" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    return [_row(r) for r in conn.execute(sql)]


def _values(zone: dict) -> tuple:
    return (
        zone["name"], zone["type"], zone["shape"], json.dumps(zone["geometry"]), zone["action"],
        json.dumps(zone["weekdays"]) if zone["weekdays"] else None, zone["time_window"],
        zone["volume_exception"], zone["event_min"], zone["place_name"], zone["label"], int(zone["enabled"]),
    )  # fmt: skip


_COLS = (
    "name, type, shape, geometry_json, action, weekdays_json, time_window, volume_exception, "
    "event_min, place_name, label, enabled"
)


def create(conn: sqlite3.Connection, data: dict) -> dict:
    zone = validate(data)
    cur = conn.execute(f"INSERT INTO zone({_COLS}) VALUES({','.join('?' * 12)})", _values(zone))
    conn.commit()
    return {"id": int(cur.lastrowid), **zone}


def update(conn: sqlite3.Connection, zone_id: int, data: dict) -> dict | None:
    if conn.execute("SELECT 1 FROM zone WHERE id=?", (zone_id,)).fetchone() is None:
        return None
    zone = validate(data)
    sets = ", ".join(f"{c.strip()}=?" for c in _COLS.split(","))
    conn.execute(f"UPDATE zone SET {sets} WHERE id=?", (*_values(zone), zone_id))
    conn.commit()
    return {"id": zone_id, **zone}


def delete(conn: sqlite3.Connection, zone_id: int) -> bool:
    cur = conn.execute("DELETE FROM zone WHERE id=?", (zone_id,))
    conn.commit()
    return cur.rowcount > 0


def contains(zone: dict, point: tuple[float, float] | None) -> bool:
    if point is None:
        return False
    geo = zone["geometry"]
    if zone["shape"] == "circle":
        return haversine_km(point, (geo["lat"], geo["lon"])) * 1000 <= geo["radius_m"]
    lat, lon = point
    inside = False
    pts = geo["points"]
    j = len(pts) - 1
    for i in range(len(pts)):
        (lat_i, lon_i), (lat_j, lon_j) = pts[i], pts[j]
        if (lon_i > lon) != (lon_j > lon) and lat < (lat_j - lat_i) * (lon - lon_i) / (lon_j - lon_i) + lat_i:
            inside = not inside
        j = i
    return inside


def applies(zone: dict, point: tuple[float, float] | None, local_dt: datetime | None) -> bool:
    """Inside the zone, on the right weekday and within the time window."""
    if not zone["enabled"] or not contains(zone, point):
        return False
    if zone["weekdays"] or zone["time_window"]:
        if local_dt is None:
            return False
        if zone["weekdays"] and local_dt.weekday() not in zone["weekdays"]:
            return False
        if zone["time_window"] and not _window(zone["time_window"], local_dt.time()):
            return False
    return True


def at(zones: list[dict], point: tuple[float, float] | None, kinds: set[str]) -> dict | None:
    for zone in zones:
        if zone["type"] in kinds and zone["enabled"] and contains(zone, point):
            return zone
    return None
