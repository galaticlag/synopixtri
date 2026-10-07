"""Decide where every ready unit goes. Pure decision code: nothing is moved here."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from . import folders, naming, rules, zones
from .config import Paths
from .geo import centroid, haversine_km
from .models import Unit

Geocoder = Callable[[float, float], "str | None"]
NEVER = 10**9  # an event threshold nobody reaches


@dataclass
class Target:
    role: str  # routine_mois | evenement | sejour | review | quarantine | aside | route
    path: Path
    name: str
    tracked: bool = True  # False for review / quarantine / aside / route (plain drop folders)
    new: bool = True
    row_id: int | None = None
    date_start: date | None = None
    date_end: date | None = None
    month_key: str | None = None
    lat: float | None = None
    lon: float | None = None
    rename_to: str | None = None  # new basename when widening a folder we manage


@dataclass
class Move:
    src: Path
    target: Target
    live: bool
    unit_key: str
    ref_date: date | None
    reason: str
    content_id: str | None = None
    placed_id: int | None = None  # promotion of a file we already placed
    why: str = ""  # human explanation stored in the journal


@dataclass
class Proposal:
    """A new event waiting for the user's approval (validation mode)."""

    start: date
    end: date
    point: tuple[float, float] | None
    place: str | None
    count: int
    paths: list[str]


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    held: list[tuple[str, str]] = field(default_factory=list)  # (unit key, reason)
    proposals: list[Proposal] = field(default_factory=list)

    def targets(self) -> list[Target]:
        seen: dict[int, Target] = {}
        for move in self.moves:
            seen.setdefault(id(move.target), move.target)
        return list(seen.values())


@dataclass
class _Day:
    day: date
    units: list[Unit]
    point: tuple[float, float] | None
    total: int
    threshold: int
    last_arrival: float


@dataclass
class _Group:
    days: list[date]
    points: list[tuple[float, float]]
    approval: dict | None = None

    @property
    def start(self) -> date:
        return self.days[0]

    @property
    def end(self) -> date:
        return self.days[-1]

    @property
    def point(self) -> tuple[float, float] | None:
        return centroid(self.points)


class Planner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: SimpleNamespace,
        paths: Paths,
        now: float,
        tz,
        geocoder: Geocoder,
        dups: dict[str, str],
        near: set[str] | None = None,
        exempt: set[str] | None = None,
    ) -> None:
        self.conn, self.cfg, self.paths, self.now, self.tz = conn, cfg, paths, now, tz
        self.geocoder, self.dups = geocoder, dups
        self.near, self.exempt = near or set(), exempt or set()
        self.zone_list = zones.load(conn, enabled_only=True)
        self.rule_list = rules.load(conn, enabled_only=True)
        self.approvals = [
            {
                "start": date.fromisoformat(r["start_date"]), "end": date.fromisoformat(r["end_date"]),
                "action": r["action"], "label": r["label"], "place": r["place"],
            }
            for r in conn.execute("SELECT * FROM approval ORDER BY id")
        ]  # fmt: skip
        self.notes: dict[str, str] = {}  # unit key -> tags recorded by "tag" rules
        self.plan = Plan()
        self._targets: dict[tuple[str, str], Target] = {}
        self.today = datetime.fromtimestamp(now, tz).date()
        self.home = (
            (cfg.home_lat, cfg.home_lon) if cfg.home_lat is not None and cfg.home_lon is not None else None
        )

    # -- targets ---------------------------------------------------------------------------
    def _get(self, target: Target) -> Target:
        return self._targets.setdefault((target.role, str(target.path)), target)

    def _drop(self, role: str, folder: Path, name: str) -> Target:
        return self._get(Target(role, folder, name, tracked=False))

    def _from_row(self, row: sqlite3.Row) -> Target:
        path = Path(row["path"])
        return self._get(
            Target(
                row["role"], path, path.name, new=False, row_id=row["id"],
                date_start=date.fromisoformat(row["date_start"]) if row["date_start"] else None,
                date_end=date.fromisoformat(row["date_end"]) if row["date_end"] else None,
                month_key=row["month_key"], lat=row["lat"], lon=row["lon"],
            )
        )  # fmt: skip

    def _routine(self, day: date) -> Target:
        key = naming.month_key(day)
        row = folders.find_routine(self.conn, key)
        if row:
            return self._from_row(row)
        name = naming.routine_name(key, self.cfg.routine_label, self.cfg.routine_template)
        path = self.paths.library / f"{day.year:04d}" / name
        return self._get(
            Target("routine_mois", path, name, new=not path.exists(), month_key=key,
                   date_start=day.replace(day=1), date_end=day.replace(day=1))
        )  # fmt: skip

    def _event(
        self, start: date, end: date, point: tuple[float, float] | None,
        label: str | None = None, place: str | None = None,
    ) -> Target:
        zone = zones.at(self.zone_list, point, {"home", "frequent"})
        place = place or (zone["place_name"] if zone else None) or (self.geocoder(*point) if point else None)
        label = label or (zone["label"] if zone else None) or self.cfg.placeholder_label
        name = naming.event_name(start, end, label, place or self.cfg.unknown_place, self.cfg.event_template)
        path = self.paths.library / f"{start.year:04d}" / name
        existing = self.conn.execute("SELECT * FROM folder WHERE path=?", (str(path),)).fetchone()
        if existing:
            return self._from_row(existing)
        role = "evenement" if start == end else "sejour"
        lat, lon = point if point else (None, None)
        return self._get(
            Target(role, path, name, new=not path.exists(), date_start=start, date_end=end, lat=lat, lon=lon)
        )

    # -- placing units -----------------------------------------------------------------------
    def _add(self, unit: Unit, target: Target, reason: str, why: str = "") -> None:
        ref = unit.ref_dt.date() if unit.ref_dt else None
        cid = unit.meta.content_id if unit.meta else None
        why = why + self.notes.get(unit.key, "")
        if unit.live is not None:
            self.plan.moves.append(Move(unit.primary.path, target, False, unit.key, ref, reason, cid, why=why))
            self.plan.moves.append(Move(unit.live.path, target, True, unit.key, ref, reason, cid, why=why))
        else:
            live = unit.kind == "orphan_live_mov" and self.cfg.orphan_live_policy == "live_subfolder"
            self.plan.moves.append(Move(unit.primary.path, target, live, unit.key, ref, reason, cid, why=why))

    def _review(self, unit: Unit, reason: str, why: str = "") -> None:
        folder = self.paths.review / reason
        self._add(unit, self._drop("review", folder, reason), reason, why)

    def _aside(self, unit: Unit, folder_name: str, reason: str, why: str) -> None:
        self._add(unit, self._drop("aside", self.paths.aside / folder_name, folder_name), reason, why)

    def _hold(self, units: list[Unit], reason: str) -> None:
        self.plan.held += [(u.key, reason) for u in units]

    # -- rules and zones -----------------------------------------------------------------------
    def _subject(self, unit: Unit) -> rules.Subject:
        try:
            rel = str(unit.primary.path.parent.relative_to(self.paths.inbox)).replace("\\", "/")
        except ValueError:
            rel = ""
        return rules.Subject(
            unit.primary.path, unit.primary.size, unit.meta, "" if rel == "." else rel,
            unit.primary.analysis, unit.key in self.dups, unit.key in self.near,
        )  # fmt: skip

    def _apply_rule(self, unit: Unit, rule: dict) -> None:
        why = rules.why(rule)
        name = rules.slug(rule["name"])
        action = rule["action"]
        if action == "aside":
            self._aside(unit, name, "aside_rule", why)
        elif action == "ignore":
            self.plan.held.append((unit.key, "ignored_by_rule"))
        elif action == "review":
            self._review(unit, f"regle_{name}", why)
        elif action == "route":
            when = unit.ref_dt or datetime.fromtimestamp(unit.primary.mtime_ns / 1e9, self.tz)
            sub = rule["params"]["folder"].format(year=f"{when.year:04d}", month=f"{when.month:02d}", day=f"{when.day:02d}")
            self._add(unit, self._drop("route", self.paths.library / sub, sub), "route_rule", why)

    def _zone_hits(self, units: list[Unit]) -> tuple[dict[str, dict], dict[tuple[int, date | None], int]]:
        """Work / exclusion zones that apply to each unit, and how many media hit a zone per day."""
        hits: dict[str, dict] = {}
        counts: dict[tuple[int, date | None], int] = defaultdict(int)
        areas = [z for z in self.zone_list if z["type"] in ("work", "exclusion")]
        for unit in units:
            if not areas or unit.gps is None or unit.key in self.exempt or unit.key in self.dups:
                continue
            if unit.meta is None or unit.meta.error:
                continue
            for zone in areas:
                if zones.applies(zone, unit.gps, unit.ref_dt):
                    hits[unit.key] = zone
                    counts[(zone["id"], unit.ref_dt.date() if unit.ref_dt else None)] += 1
                    break
        return hits, counts

    def _zone_unit(self, unit: Unit, zone: dict, count: int) -> None:
        name = f"zone_{rules.slug(zone['name'])}"
        where = f"Zone « {zone['name']} » ({zones.LABELS[zone['type']].lower()})"
        limit = zone["volume_exception"]
        if limit and count >= limit:
            self._review(unit, name, f"{where}: {count} médias ce jour (seuil {limit}), à vérifier plutôt que mis de côté")
        elif zone["action"] == "aside":
            self._aside(unit, name, "aside_zone", f"{where}: mis de côté")
        elif zone["action"] == "leave":
            self.plan.held.append((unit.key, "zone_leave"))
        else:
            self._review(unit, name, f"{where}: envoyé en revue")

    # -- the algorithm -----------------------------------------------------------------------
    def run(self, units: list[Unit]) -> Plan:
        cfg = self.cfg
        zone_of, zone_count = self._zone_hits(units)
        dated: list[Unit] = []
        for unit in units:
            exempt = unit.key in self.exempt
            if not exempt and self.rule_list:
                tags, rule = rules.evaluate(self.rule_list, self._subject(unit), cfg, self.zone_list)
                if tags:
                    self.notes[unit.key] = " [étiquettes: " + ", ".join(tags) + "]"
                if rule is not None:
                    self._apply_rule(unit, rule)
                    continue
            if unit.key in self.dups:
                reason = self.dups[unit.key]
                why = (
                    "Doublon exact d'un fichier déjà classé dans la bibliothèque"
                    if reason == "duplicate_of_library" else "Doublon exact d'un autre fichier du même lot"
                )  # fmt: skip
                self._add(unit, self._drop("quarantine", self.paths.quarantine, "quarantine"), reason, why)
            elif unit.meta is None or unit.meta.error:
                self._review(unit, "exif_error", "Métadonnées illisibles")
            elif unit.key in zone_of:
                zone = zone_of[unit.key]
                day = unit.ref_dt.date() if unit.ref_dt else None
                self._zone_unit(unit, zone, zone_count[(zone["id"], day)])
            elif not unit.reliable:
                self._review(unit, "no_date", "Aucune date fiable dans le fichier")
            elif unit.kind == "orphan_live_mov" and cfg.orphan_live_policy == "review":
                self._review(unit, "orphan_live", "Vidéo Live sans sa photo (politique: revue)")
            else:
                dated.append(unit)

        by_day: dict[date, list[Unit]] = defaultdict(list)
        for unit in dated:
            by_day[unit.ref_dt.date()].append(unit)  # type: ignore[union-attr]

        days: dict[date, _Day] = {}
        for d, day_units in by_day.items():
            point = centroid([u.gps for u in day_units if u.gps])
            placed = folders.placed_units_on(self.conn, d)
            total = len(day_units) + sum(placed.values())
            zone = zones.at(self.zone_list, point, {"home", "frequent"})
            away = bool(self.home and point and haversine_km(point, self.home) > cfg.home_radius_km)
            threshold = cfg.away_event_min_items if away else cfg.event_min_items
            if zone is not None:
                if zone["event_min"] is not None:
                    threshold = zone["event_min"] if zone["event_min"] > 0 else NEVER
                elif zone["type"] == "home":
                    threshold = cfg.event_min_items
            days[d] = _Day(d, day_units, point, total, threshold, max(u.last_arrival for u in day_units))

        # 1. Late arrivals for a period that already has a folder.
        remaining: dict[date, _Day] = {}
        for d in sorted(days):
            row = folders.find_covering(self.conn, d, days[d].point, cfg.stay_radius_km)
            if row is None:
                remaining[d] = days[d]
                continue
            if row["status"] == "verrouille":
                for unit in days[d].units:
                    self._review(unit, "folder_locked", f"Le dossier « {Path(row['path']).name} » est verrouillé")
                continue
            target = self._from_row(row)
            for unit in days[d].units:
                self._add(unit, target, "late_attach", f"Période déjà couverte par le dossier « {target.name} »")

        # 2. Days with enough media become events or stays; approvals override the automatic grouping.
        claimed: set[date] = set()
        manual: list[_Group] = []
        for ap in self.approvals:
            ds = [d for d in sorted(remaining) if ap["start"] <= d <= ap["end"] and d not in claimed]
            if not ds:
                continue
            claimed.update(ds)
            if ap["action"] == "event":
                manual.append(_Group(ds, [remaining[d].point for d in ds if remaining[d].point], ap))
        qualifying = [
            d for d in sorted(remaining) if d not in claimed and remaining[d].total >= remaining[d].threshold
        ]
        groups = [
            (g, [d for d in sorted(remaining) if g.start <= d <= g.end and d not in claimed])
            for g in self._groups(qualifying, remaining)
        ]
        groups += [(g, g.days) for g in manual]
        groups.sort(key=lambda pair: pair[0].start)
        for group, members in groups:
            member_units = [u for d in members for u in remaining[d].units]
            if not self._mature(group, [remaining[d] for d in members]):
                self._hold(member_units, "maturing")
            elif group.approval is None and cfg.validation_mode and self._needs_validation(group):
                self._propose(group, member_units)
                self._hold(member_units, "awaiting_validation")
            else:
                target = self._group_target(group)
                first = remaining[members[0]]
                if group.approval is not None:
                    why = "Regroupement validé par l'utilisateur"
                elif group.start == group.end:
                    why = f"{first.total} médias ce jour (seuil {first.threshold}): événement"
                else:
                    why = f"Séjour de {len(members)} jours consécutifs, chacun au-dessus du seuil"
                for unit in member_units:
                    self._add(unit, target, "event" if group.start == group.end else "stay", why)
                self._promote(group, target)
            for d in members:
                remaining.pop(d, None)

        # 3. Everything else is routine, filed in the monthly folder.
        for d in sorted(remaining):
            day = remaining[d]
            if not cfg.routine_immediate and not self._day_mature(day):
                self._hold(day.units, "maturing")
                continue
            row = folders.find_routine(self.conn, naming.month_key(d))
            if row is not None and row["status"] == "verrouille":
                for unit in day.units:
                    self._review(unit, "folder_locked", f"Le dossier « {Path(row['path']).name} » est verrouillé")
                continue
            target = self._routine(d)
            why = f"{day.total} médias ce jour (seuil {day.threshold}): dossier mensuel"
            for unit in day.units:
                self._add(unit, target, "routine", why)
        return self.plan

    def _groups(self, qualifying: list[date], days: dict[date, _Day]) -> list[_Group]:
        groups: list[_Group] = []
        for d in qualifying:
            point = days[d].point
            cur = groups[-1] if groups else None
            if (
                cur is not None
                and (d - cur.end).days - 1 <= self.cfg.max_gap_days_in_stay
                and (point is None or cur.point is None or haversine_km(point, cur.point) <= self.cfg.stay_radius_km)
            ):
                cur.days.append(d)
                if point:
                    cur.points.append(point)
            else:
                groups.append(_Group([d], [point] if point else []))
        return groups

    def _day_mature(self, day: _Day) -> bool:
        return (
            self.now - day.last_arrival >= self.cfg.day_maturity_hours * 3600 and day.day < self.today
        )

    def _mature(self, group: _Group, days: list[_Day]) -> bool:
        last = max(d.last_arrival for d in days)
        quiet = self.now - last
        if quiet < self.cfg.day_maturity_hours * 3600 or group.end >= self.today:
            return False
        if group.start != group.end:
            return (
                quiet >= self.cfg.stay_quiet_hours * 3600
                and (self.today - group.end).days >= self.cfg.stay_maturity_days
            )
        return True

    def _adjacent_row(self, group: _Group) -> sqlite3.Row | None:
        cfg = self.cfg
        row = folders.find_adjacent(self.conn, group.start, group.point, cfg.stay_radius_km, cfg.max_gap_days_in_stay)
        return row or folders.find_adjacent(self.conn, group.end, group.point, cfg.stay_radius_km, cfg.max_gap_days_in_stay)

    def _needs_validation(self, group: _Group) -> bool:
        """Extending an existing event is routine; a brand new one waits for the user."""
        return self._adjacent_row(group) is None

    def _propose(self, group: _Group, units: list[Unit]) -> None:
        point = group.point
        place = self.geocoder(*point) if point else None
        self.plan.proposals.append(
            Proposal(group.start, group.end, point, place, len(units), [u.key for u in units])
        )

    def _group_target(self, group: _Group) -> Target:
        cfg = self.cfg
        ap = group.approval or {}
        row = self._adjacent_row(group)
        if row is None:
            return self._event(group.start, group.end, group.point, ap.get("label"), ap.get("place"))
        target = self._from_row(row)
        new_start = min(target.date_start or group.start, group.start)
        new_end = max(target.date_end or group.end, group.end)
        target.date_start, target.date_end = new_start, new_end
        if row["status"] == "gere":
            parsed = naming.parse_name(Path(row["path"]).name, cfg.routine_label, cfg.event_template, cfg.routine_template)
            label = parsed.label if parsed else cfg.placeholder_label
            place = parsed.place if parsed and parsed.place else cfg.unknown_place
            new_name = naming.event_name(new_start, new_end, label, place, cfg.event_template)
            if new_name != target.name:
                target.rename_to = new_name
        return target

    def _promote(self, group: _Group, target: Target) -> None:
        """Pull already-filed routine media of the same days into the new event folder."""
        for row in folders.placed_routine_between(self.conn, group.start, group.end):
            src = Path(row["folder_path"]) / row["rel_path"]
            self.plan.moves.append(
                Move(src, target, bool(row["is_live"]), row["unit_key"] or str(src),
                     date.fromisoformat(row["ref_date"]) if row["ref_date"] else None,
                     "promote", row["content_id"], placed_id=row["id"],
                     why="Ce jour est devenu un événement: les médias déjà classés le rejoignent")
            )  # fmt: skip


def build_plan(
    conn: sqlite3.Connection,
    cfg: SimpleNamespace,
    paths: Paths,
    units: list[Unit],
    now: float,
    tz,
    geocoder: Geocoder,
    dups: dict[str, str],
    near: set[str] | None = None,
    exempt: set[str] | None = None,
) -> Plan:
    return Planner(conn, cfg, paths, now, tz, geocoder, dups, near, exempt).run(units)
