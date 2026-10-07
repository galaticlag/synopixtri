"""User rules: "if ... then ..." evaluated before any grouping.

A rule is a list of conditions (all must hold) and one action:
  aside   move the media to the set-aside folder (reversible)
  ignore  leave them where they are
  review  send them to the review folder
  route   file them in a folder built from a pattern (under the library)
  tag     only record a label in the journal, then carry on with the next rules
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from . import analysis, zones
from .models import Meta

ACTIONS = {"aside", "ignore", "review", "route", "tag"}
ACTION_LABELS = {
    "aside": "mettre de côté", "ignore": "ignorer (laisser en place)", "review": "envoyer en revue",
    "route": "classer dans un dossier", "tag": "étiqueter",
}  # fmt: skip
ANALYSIS_FLAGS = set(analysis.FLAGS)
_NEEDS_ANALYSIS = {"blur", "dark", "document", "near_duplicate"}

TEMPLATES = [
    {
        "id": "screenshots", "name": "Captures d'écran",
        "description": "Images nommées « Screenshot », « Capture d'écran »… ou PNG sans appareil photo.",
        "conditions": [{"type": "analysis", "flag": "screenshot"}], "action": "aside", "params": {},
    },
    {
        "id": "messaging", "name": "Images de messageries",
        "description": "Fichiers reçus par WhatsApp, Signal, Telegram, Messenger.",
        "conditions": [{"type": "filename", "patterns": ["*-WA[0-9]*", "IMG-*-WA*", "signal-*", "telegram*", "FB_IMG_*", "received_*"]}],
        "action": "aside", "params": {},
    },
    {
        "id": "sidecars", "name": "Fichiers annexes (.AAE, .THM)",
        "description": "Fichiers parasites laissés par les téléphones.",
        "conditions": [{"type": "extension", "values": ["aae", "thm"]}], "action": "aside", "params": {},
    },
    {
        "id": "tiny", "name": "Toutes petites images",
        "description": "Images de moins de 30 Ko: vignettes, icônes.",
        "conditions": [{"type": "media", "value": "photo"}, {"type": "size", "max": 30000}],
        "action": "aside", "params": {},
    },
    {
        "id": "blurry", "name": "Photos floues",
        "description": "Suggestion de l'analyse: photos sans netteté, envoyées en revue.",
        "conditions": [{"type": "analysis", "flag": "blur"}], "action": "review", "params": {},
    },
    {
        "id": "dark", "name": "Photos très sombres",
        "description": "Suggestion de l'analyse: photos presque noires (poche, objectif couvert).",
        "conditions": [{"type": "analysis", "flag": "dark"}], "action": "review", "params": {},
    },
    {
        "id": "documents", "name": "Documents photographiés",
        "description": "Pages de papier, tickets, factures: classées à part.",
        "conditions": [{"type": "analysis", "flag": "document"}],
        "action": "route", "params": {"folder": "Documents/{year}"},
    },
    {
        "id": "lookalikes", "name": "Photos quasi identiques",
        "description": "Rafales et doublons visuels: seule la plus nette reste dans le tri normal.",
        "conditions": [{"type": "analysis", "flag": "near_duplicate"}], "action": "review", "params": {},
    },
]  # fmt: skip


# -- validation ------------------------------------------------------------------------------
def _iso(value) -> str | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value)).isoformat()


def _int(value) -> int | None:
    return None if value in (None, "") else int(value)


def validate_condition(cond: dict) -> dict:
    kind = cond.get("type")
    if kind == "zone":
        if cond.get("zone_id") not in (None, ""):
            return {"type": "zone", "zone_id": int(cond["zone_id"])}
        if cond.get("zone_type") in zones.TYPES:
            return {"type": "zone", "zone_type": cond["zone_type"]}
        raise ValueError("zone: give zone_id or zone_type")
    if kind == "period":
        out = {"type": "period", "from": _iso(cond.get("from")), "to": _iso(cond.get("to"))}
        if not out["from"] and not out["to"]:
            raise ValueError("period: give from and/or to")
        return out
    if kind == "weekday":
        days = sorted({int(d) for d in cond.get("days") or []})
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("weekday: days are 0 (Monday) to 6 (Sunday)")
        return {"type": "weekday", "days": days}
    if kind == "hours":
        window = str(cond.get("window") or "").strip()
        try:
            zones._window(window, datetime.min.time())  # noqa: SLF001
        except ValueError as exc:
            raise ValueError("hours: window must look like HH:MM-HH:MM") from exc
        return {"type": "hours", "window": window}
    if kind == "media":
        if cond.get("value") not in ("photo", "video"):
            raise ValueError("media: photo or video")
        return {"type": "media", "value": cond["value"]}
    if kind == "device":
        make, model = (cond.get("make") or "").strip(), (cond.get("model") or "").strip()
        if not make and not model:
            raise ValueError("device: give make and/or model")
        return {"type": "device", "make": make, "model": model}
    if kind == "filename":
        patterns = [str(p).strip() for p in cond.get("patterns") or [] if str(p).strip()]
        if not patterns:
            raise ValueError("filename: give at least one pattern (* and ? allowed)")
        return {"type": "filename", "patterns": patterns}
    if kind == "extension":
        values = [str(v).lower().lstrip(".") for v in cond.get("values") or [] if str(v).strip()]
        if not values:
            raise ValueError("extension: give at least one extension")
        return {"type": "extension", "values": values}
    if kind == "source":
        folder = str(cond.get("folder", "")).strip().strip("/\\").replace("\\", "/")
        if not folder or ".." in folder.split("/"):
            raise ValueError("source: give a sub-folder of the inbox")
        return {"type": "source", "folder": folder}
    if kind == "size":
        lo, hi = _int(cond.get("min")), _int(cond.get("max"))
        if lo is None and hi is None:
            raise ValueError("size: give min and/or max (bytes)")
        return {"type": "size", "min": lo, "max": hi}
    if kind == "dimensions":
        out = {k: _int(cond.get(k)) for k in ("min_width", "min_height", "max_width", "max_height")}
        if all(v is None for v in out.values()):
            raise ValueError("dimensions: give at least one bound (pixels)")
        return {"type": "dimensions", **out}
    if kind == "duplicate":
        return {"type": "duplicate", "value": bool(cond.get("value", True))}
    if kind == "gps":
        return {"type": "gps", "value": bool(cond.get("value", True))}
    if kind == "analysis":
        if cond.get("flag") not in ANALYSIS_FLAGS:
            raise ValueError(f"analysis: flag must be one of {sorted(ANALYSIS_FLAGS)}")
        return {"type": "analysis", "flag": cond["flag"]}
    raise ValueError(f"unknown condition type {kind!r}")


def validate(data: dict) -> dict:
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("a rule needs a name")
    action = str(data.get("action", ""))
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")
    conditions = [validate_condition(c) for c in data.get("conditions") or []]
    if not conditions:
        raise ValueError("a rule needs at least one condition")
    params = dict(data.get("params") or {})
    if action == "route":
        folder = str(params.get("folder", "")).strip().strip("/\\").replace("\\", "/")
        if not folder or ":" in folder or ".." in folder.split("/") or re.sub(r"\{(year|month|day)\}", "", folder).count("{"):
            raise ValueError("route: give a folder pattern, tokens {year} {month} {day}")
        params = {"folder": folder}
    elif action == "tag":
        tag = str(params.get("tag", "")).strip()
        if not tag:
            raise ValueError("tag: give a label")
        params = {"tag": tag}
    else:
        params = {}
    return {"name": name, "enabled": bool(data.get("enabled", True)), "conditions": conditions, "action": action, "params": params}


def describe(cond: dict) -> str:
    kind = cond["type"]
    if kind == "zone":
        if "zone_id" in cond:
            return f"dans la zone n°{cond['zone_id']}"
        return f"dans une zone de type « {zones.LABELS[cond['zone_type']]} »"
    if kind == "period":
        parts = []
        if cond.get("from"):
            parts.append(f"à partir du {cond['from']}")
        if cond.get("to"):
            parts.append(f"jusqu'au {cond['to']}")
        return "pris " + " ".join(parts)
    if kind == "weekday":
        names = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        return "pris le " + "/".join(names[d] for d in cond["days"])
    if kind == "hours":
        return f"pris entre {cond['window'].replace('-', ' et ')}"
    if kind == "media":
        return "est une photo" if cond["value"] == "photo" else "est une vidéo"
    if kind == "device":
        return "appareil « " + " ".join(x for x in (cond["make"], cond["model"]) if x) + " »"
    if kind == "filename":
        return "nom du fichier « " + " ou ".join(cond["patterns"]) + " »"
    if kind == "extension":
        return "extension " + "/".join(cond["values"])
    if kind == "source":
        return f"dans le sous-dossier « {cond['folder']} » de l'arrivée"
    if kind == "size":
        parts = []
        if cond.get("min") is not None:
            parts.append(f"au moins {cond['min'] // 1000} Ko")
        if cond.get("max") is not None:
            parts.append(f"au plus {cond['max'] // 1000} Ko")
        return "taille " + " et ".join(parts)
    if kind == "dimensions":
        return "dimensions " + ", ".join(f"{k.replace('_', ' ')} {v}px" for k, v in cond.items() if k != "type" and v is not None)
    if kind == "duplicate":
        return "est un doublon exact" if cond["value"] else "n'est pas un doublon"
    if kind == "gps":
        return "a une position GPS" if cond["value"] else "n'a pas de position GPS"
    labels = {
        "blur": "floue", "dark": "très sombre", "screenshot": "ressemble à une capture d'écran",
        "document": "ressemble à un document", "near_duplicate": "quasi identique à une autre photo",
    }  # fmt: skip
    return "analyse: " + labels[cond["flag"]]


def needs_analysis(rules: list[dict]) -> bool:
    return any(
        c["type"] == "analysis" and c["flag"] in _NEEDS_ANALYSIS for r in rules if r["enabled"] for c in r["conditions"]
    )


# -- evaluation ------------------------------------------------------------------------------
@dataclass
class Subject:
    path: Path
    size: int
    meta: Meta | None
    rel_source: str = ""
    analysis: dict | None = None
    is_duplicate: bool = False
    near_duplicate: bool = False
    _flags: set[str] | None = field(default=None, repr=False)

    @property
    def point(self) -> tuple[float, float] | None:
        if self.meta and self.meta.lat is not None and self.meta.lon is not None:
            return (self.meta.lat, self.meta.lon)
        return None

    @property
    def local_dt(self) -> datetime | None:
        return self.meta.local_dt if self.meta and not self.meta.error else None

    def flags(self, cfg) -> set[str]:
        if self._flags is None:
            self._flags = analysis.flags(self.path.name, self.meta, self.analysis, cfg)
            if self.near_duplicate:
                self._flags.add("near_duplicate")
        return self._flags


def matches(cond: dict, s: Subject, cfg: SimpleNamespace, zone_list: list[dict]) -> bool:
    kind = cond["type"]
    when = s.local_dt
    if kind == "zone":
        if s.point is None:
            return False
        for z in zone_list:
            if "zone_id" in cond and z["id"] != cond["zone_id"]:
                continue
            if "zone_type" in cond and z["type"] != cond["zone_type"]:
                continue
            if zones.applies(z, s.point, when):
                return True
        return False
    if kind == "period":
        if when is None:
            return False
        day = when.date().isoformat()
        return (not cond.get("from") or day >= cond["from"]) and (not cond.get("to") or day <= cond["to"])
    if kind == "weekday":
        return when is not None and when.weekday() in cond["days"]
    if kind == "hours":
        return when is not None and zones._window(cond["window"], when.time())  # noqa: SLF001
    if kind == "media":
        is_video = bool(s.meta and s.meta.is_video)
        return is_video == (cond["value"] == "video")
    if kind == "device":
        m = s.meta
        if m is None:
            return False
        make_ok = not cond["make"] or cond["make"].lower() in (m.make or "").lower()
        model_ok = not cond["model"] or cond["model"].lower() in (m.model or "").lower()
        return make_ok and model_ok
    if kind == "filename":
        name = s.path.name.lower()
        return any(fnmatch.fnmatchcase(name, p.lower()) for p in cond["patterns"])
    if kind == "extension":
        return s.path.suffix.lower().lstrip(".") in cond["values"]
    if kind == "source":
        return s.rel_source == cond["folder"] or s.rel_source.startswith(cond["folder"] + "/")
    if kind == "size":
        return (cond.get("min") is None or s.size >= cond["min"]) and (cond.get("max") is None or s.size <= cond["max"])
    if kind == "dimensions":
        w = (s.meta.width if s.meta and s.meta.width else None) or (s.analysis or {}).get("w")
        h = (s.meta.height if s.meta and s.meta.height else None) or (s.analysis or {}).get("h")
        if w is None or h is None:
            return False
        return (
            (cond.get("min_width") is None or w >= cond["min_width"])
            and (cond.get("min_height") is None or h >= cond["min_height"])
            and (cond.get("max_width") is None or w <= cond["max_width"])
            and (cond.get("max_height") is None or h <= cond["max_height"])
        )  # fmt: skip
    if kind == "duplicate":
        return s.is_duplicate == cond["value"]
    if kind == "gps":
        return (s.point is not None) == cond["value"]
    if kind == "analysis":
        return cond["flag"] in s.flags(cfg)
    return False


def evaluate(
    rules: list[dict], s: Subject, cfg: SimpleNamespace, zone_list: list[dict]
) -> tuple[list[str], dict | None]:
    """Return (tags collected, the first deciding rule or None). Rules are tried in order."""
    tags: list[str] = []
    for rule in rules:
        if not rule["enabled"] or not all(matches(c, s, cfg, zone_list) for c in rule["conditions"]):
            continue
        if rule["action"] == "tag":
            tags.append(rule["params"]["tag"])
            continue
        return tags, rule
    return tags, None


def why(rule: dict) -> str:
    return f"Règle « {rule['name']} »: " + ", ".join(describe(c) for c in rule["conditions"])


def slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", plain.lower()).strip("_") or "regle"


# -- storage ---------------------------------------------------------------------------------
def _row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]), "position": row["position"],
        "conditions": json.loads(row["conditions_json"]), "action": row["action"],
        "params": json.loads(row["params_json"]) if row["params_json"] else {},
    }  # fmt: skip


def load(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM rule" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY position, id"
    return [_row(r) for r in conn.execute(sql)]


def create(conn: sqlite3.Connection, data: dict) -> dict:
    rule = validate(data)
    pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM rule").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO rule(name, enabled, position, conditions_json, action, params_json) VALUES(?,?,?,?,?,?)",
        (rule["name"], int(rule["enabled"]), pos, json.dumps(rule["conditions"]), rule["action"], json.dumps(rule["params"])),
    )
    conn.commit()
    return {"id": int(cur.lastrowid), "position": pos, **rule}


def update(conn: sqlite3.Connection, rule_id: int, data: dict) -> dict | None:
    row = conn.execute("SELECT position FROM rule WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        return None
    rule = validate(data)
    conn.execute(
        "UPDATE rule SET name=?, enabled=?, conditions_json=?, action=?, params_json=? WHERE id=?",
        (rule["name"], int(rule["enabled"]), json.dumps(rule["conditions"]), rule["action"], json.dumps(rule["params"]), rule_id),
    )
    conn.commit()
    return {"id": rule_id, "position": row["position"], **rule}


def delete(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM rule WHERE id=?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def reorder(conn: sqlite3.Connection, ids: list[int]) -> None:
    for pos, rule_id in enumerate(ids, start=1):
        conn.execute("UPDATE rule SET position=? WHERE id=?", (pos, int(rule_id)))
    conn.commit()


# -- versions ----------------------------------------------------------------------------------
def snapshot(conn: sqlite3.Connection, note: str, *, keep: int = 50) -> int:
    data = {"zones": zones.load(conn), "rules": load(conn)}
    cur = conn.execute(
        "INSERT INTO rule_version(created_at, note, data_json) VALUES(?,?,?)", (time.time(), note, json.dumps(data))
    )
    conn.execute("DELETE FROM rule_version WHERE id <= ?", (int(cur.lastrowid) - keep,))
    conn.commit()
    return int(cur.lastrowid)


def latest_version(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MAX(id) FROM rule_version").fetchone()[0]


def versions(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"id": r["id"], "created_at": r["created_at"], "note": r["note"]}
        for r in conn.execute("SELECT id, created_at, note FROM rule_version ORDER BY id DESC LIMIT 50")
    ]


def restore(conn: sqlite3.Connection, version_id: int) -> bool:
    row = conn.execute("SELECT data_json FROM rule_version WHERE id=?", (version_id,)).fetchone()
    if row is None:
        return False
    data = json.loads(row["data_json"])
    conn.execute("DELETE FROM zone")
    conn.execute("DELETE FROM rule")
    for z in data["zones"]:
        conn.execute(
            f"INSERT INTO zone(id, {zones._COLS}) VALUES(?,{','.join('?' * 12)})",  # noqa: SLF001
            (z["id"], *zones._values(z)),  # noqa: SLF001
        )
    for r in data["rules"]:
        conn.execute(
            "INSERT INTO rule(id, name, enabled, position, conditions_json, action, params_json) VALUES(?,?,?,?,?,?,?)",
            (r["id"], r["name"], int(r["enabled"]), r["position"], json.dumps(r["conditions"]), r["action"], json.dumps(r["params"])),
        )
    conn.commit()
    snapshot(conn, f"Retour à la version {version_id}")
    return True


# -- live preview ------------------------------------------------------------------------------
def inbox_subjects(conn: sqlite3.Connection, cfg: SimpleNamespace, inbox: Path, *, compute_cap: int = 0) -> list[Subject]:
    """Every inbox file seen so far as a rule subject (analysis computed up to ``compute_cap``)."""
    subjects: list[Subject] = []
    computed = 0
    for row in conn.execute("SELECT path, size, meta_json, analysis_json FROM inbox_file").fetchall():
        path = Path(row["path"])
        meta = Meta.from_dict(json.loads(row["meta_json"])) if row["meta_json"] else None
        data = json.loads(row["analysis_json"]) if row["analysis_json"] else None
        if data is None and computed < compute_cap:
            data = analysis.analyze(path)
            computed += 1
            if data:
                conn.execute("UPDATE inbox_file SET analysis_json=? WHERE path=?", (json.dumps(data), row["path"]))
        try:
            rel = str(path.parent.relative_to(inbox)).replace("\\", "/")
        except ValueError:
            rel = ""
        subjects.append(Subject(path, row["size"], meta, "" if rel == "." else rel, data))
    conn.commit()
    flagged, _groups = analysis.near_duplicates(
        [(str(s.path), s.analysis) for s in subjects if s.analysis], cfg.near_duplicate_distance
    )
    for s in subjects:
        s.near_duplicate = str(s.path) in flagged
    return subjects


def preview(conn: sqlite3.Connection, cfg: SimpleNamespace, inbox: Path, conditions: list[dict], *, limit: int = 8) -> dict:
    """How many inbox files a rule would catch right now, with a few examples."""
    clean = [validate_condition(c) for c in conditions]
    zone_list = zones.load(conn, enabled_only=True)
    wants = any(c["type"] == "analysis" and c["flag"] in _NEEDS_ANALYSIS for c in clean)
    subjects = inbox_subjects(conn, cfg, inbox, compute_cap=100 if wants else 0)
    hits = [s for s in subjects if all(matches(c, s, cfg, zone_list) for c in clean)]
    return {
        "count": len(hits),
        "total": len(subjects),
        "examples": [str(s.path.relative_to(inbox)).replace("\\", "/") if inbox in s.path.parents else s.path.name for s in hits[:limit]],
        "description": [describe(c) for c in clean],
    }
