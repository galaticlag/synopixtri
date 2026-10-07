"""Folder names: building them and parsing existing ones."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


EVENT_DEFAULT = "{range} {label}, {place}"
ROUTINE_DEFAULT = "{year}.{month} {label}"

_TOKEN = re.compile(r"\{(\w+)\}")
_TOKENS = {"event": ({"range", "label", "place"}, {"range", "label"}), "routine": ({"year", "month", "label"}, {"year", "month", "label"})}


def validate_template(kind: str, text: str) -> None:
    """Raise ValueError unless ``text`` is a usable folder-name template."""
    allowed, required = _TOKENS[kind]
    found = _TOKEN.findall(text)
    unknown = set(found) - allowed
    if unknown:
        raise ValueError(f"unknown token {sorted(unknown)[0]!r}, allowed: {sorted(allowed)}")
    missing = required - set(found)
    if missing:
        raise ValueError(f"the template must contain {{{sorted(missing)[0]}}}")
    if len(found) != len(set(found)):
        raise ValueError("each token can only be used once")
    rest = _TOKEN.sub("", text)
    if "{" in rest or "}" in rest or "/" in text or "\\" in text or not text.strip():
        raise ValueError("invalid template (no braces outside tokens, no slashes)")
    if kind == "event" and "place" in found and found.index("place") < found.index("label"):
        raise ValueError("{place} must come after {label}")


def render(template: str, **values: str) -> str:
    return sanitize(_TOKEN.sub(lambda m: values.get(m.group(1), ""), template))


def sanitize(text: str) -> str:
    text = _ILLEGAL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:100]


def format_range(start: date, end: date) -> str:
    """Compress a date range: 2021.01.13~16, 2021.07.21~08.10, 2024.12.28~2025.01.03."""
    s = f"{start.year:04d}.{start.month:02d}.{start.day:02d}"
    if start == end:
        return s
    if (start.year, start.month) == (end.year, end.month):
        return f"{s}~{end.day:02d}"
    if start.year == end.year:
        return f"{s}~{end.month:02d}.{end.day:02d}"
    return f"{s}~{end.year:04d}.{end.month:02d}.{end.day:02d}"


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def routine_name(key: str, label: str, template: str = ROUTINE_DEFAULT) -> str:
    year, month = key.split("-")
    return render(template, year=year, month=month, label=label)


def event_name(start: date, end: date, label: str, place: str, template: str = EVENT_DEFAULT) -> str:
    return render(template, range=format_range(start, end), label=label, place=place)


@dataclass(frozen=True)
class ParsedName:
    role: str  # routine_mois | evenement | sejour
    date_start: date
    date_end: date
    label: str
    place: str | None
    month_key: str | None = None


_RANGE = (
    r"(?P<y>\d{4})\.(?P<m>\d{2})\.(?P<d>\d{2})"
    r"(?:~(?:(?P<y2>\d{4})\.)?(?:(?P<m2>\d{2})\.)?(?P<d2>\d{2}))?"
)
_cache: dict[tuple[str, str], "re.Pattern[str]"] = {}


def _compile(kind: str, template: str) -> "re.Pattern[str]":
    key = (kind, template)
    if key in _cache:
        return _cache[key]
    parts = _TOKEN.split(template)  # literal, token, literal, token, ..., literal
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(re.escape(part))
        elif part == "range":
            out.append(_RANGE)
        elif part == "year":
            out.append(r"(?P<y>\d{4})")
        elif part == "month":
            out.append(r"(?P<m>\d{2})")
        elif part == "label":
            out.append(r"(?P<label>.+?)")
        elif part == "place":
            out.append(r"(?P<place>[^,]*)")
    if kind == "event" and parts[-2:-1] == ["place"] and len(parts) >= 3 and parts[-1] == "":
        # {place} closes the name: folders without a place part are still ours.
        sep = re.escape(parts[-3])
        out[-3:] = [f"(?:{sep}{out[-2]})?"]
    pattern = re.compile("^" + "".join(out) + "$")
    _cache[key] = pattern
    return pattern


def parse_name(
    name: str,
    routine_label: str,
    event_template: str = EVENT_DEFAULT,
    routine_template: str = ROUTINE_DEFAULT,
) -> ParsedName | None:
    """Parse a folder name made with our templates. Anything else returns None."""
    m = _compile("event", event_template).match(name)
    if m:
        try:
            start = date(int(m["y"]), int(m["m"]), int(m["d"]))
            end = start
            if m["d2"]:
                end = date(int(m["y2"] or m["y"]), int(m["m2"] or m["m"]), int(m["d2"]))
        except ValueError:
            return None
        if end < start:
            return None
        place = (m.groupdict().get("place") or "").strip()
        return ParsedName(
            "evenement" if start == end else "sejour", start, end, m["label"].strip(), place or None
        )
    m = _compile("routine", routine_template).match(name)
    if m and m["label"].strip() == routine_label:
        try:
            first = date(int(m["y"]), int(m["m"]), 1)
        except ValueError:
            return None
        return ParsedName("routine_mois", first, first, routine_label, None, month_key(first))
    return None
