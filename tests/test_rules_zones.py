from __future__ import annotations

import io
import os
import time

import pytest

from synopixtri import jobs, maintenance, naming, rules, zones
from synopixtri.config import load, namespace, resolve_paths

from conftest import PARIS, photo, ts, video

NOW = ts(2026, 3, 20)  # a Friday
SUNDAY, MONDAY = (2026, 3, 1), (2026, 3, 2)
OFFICE = (48.8584, 2.2945)  # synthetic point about 5 km from PARIS


def add_rule(env, **data):
    conn = env.conn()
    try:
        return rules.create(conn, data)
    finally:
        conn.close()


def add_zone(env, **data):
    conn = env.conn()
    try:
        data.setdefault("shape", "circle")
        data.setdefault("geometry", {"lat": OFFICE[0], "lon": OFFICE[1], "radius_m": 300})
        return zones.create(conn, data)
    finally:
        conn.close()


def journal(env, job_id):
    conn = env.conn()
    rows = conn.execute("SELECT * FROM operation_log WHERE job_id=? AND op='move'", (job_id,)).fetchall()
    conn.close()
    return rows


# -- rules -------------------------------------------------------------------------------------
def test_rule_sets_files_aside_and_explains_why(env):
    add_rule(env, name="Captures d'écran", conditions=[{"type": "filename", "patterns": ["Screenshot*"]}], action="aside")
    env.add("Screenshot_1.PNG", photo(*SUNDAY))
    env.add("IMG_1.JPG", photo(*SUNDAY))
    job = env.settle(NOW)
    assert env.tree(env.root / "_mis_de_cote") == ["captures_d_ecran/Screenshot_1.PNG"]
    assert env.tree(env.library) == ["2026/2026.03 Vie de famille/IMG_1.JPG"]
    why = {r["reason"]: r["why"] for r in journal(env, job["id"])}
    assert "Captures d'écran" in why["aside_rule"] and "nom du fichier" in why["aside_rule"]
    assert "dossier mensuel" in why["routine"]


def test_aside_is_reversible_with_undo(env):
    add_rule(env, name="r", conditions=[{"type": "extension", "values": ["png"]}], action="aside")
    env.add("a.PNG", photo(*SUNDAY))
    job = env.settle(NOW)
    assert not (env.inbox / "a.PNG").exists()
    assert jobs.undo(env.boot, job["id"])["state"] == "undone"
    assert (env.inbox / "a.PNG").exists() and env.tree(env.root / "_mis_de_cote") == []


def test_ignore_rule_leaves_the_file_in_place(env):
    add_rule(env, name="Ne pas toucher", conditions=[{"type": "source", "folder": "perso"}], action="ignore")
    env.add("a.JPG", photo(*SUNDAY), sub="perso/vacances")
    env.add("b.JPG", photo(*SUNDAY))
    job = env.settle(NOW)
    assert (env.inbox / "perso" / "vacances" / "a.JPG").exists()
    assert job["stats"]["held"] == {"ignored_by_rule": 1}


def test_route_rule_files_into_a_pattern_folder(env):
    add_rule(env, name="Docs", conditions=[{"type": "filename", "patterns": ["scan_*"]}], action="route",
             params={"folder": "Documents/{year}"})  # fmt: skip
    env.add("scan_1.JPG", photo(2025, 6, 1))
    env.settle(NOW)
    assert env.tree(env.library) == ["Documents/2025/scan_1.JPG"]


def test_tag_rule_labels_the_journal_and_does_not_decide(env):
    add_rule(env, name="Tag", conditions=[{"type": "device", "make": "apple"}], action="tag", params={"tag": "iphone"})
    env.add("a.JPG", photo(*SUNDAY))
    job = env.settle(NOW)
    assert env.tree(env.library) == ["2026/2026.03 Vie de famille/a.JPG"]
    assert "étiquettes: iphone" in journal(env, job["id"])[0]["why"]


def test_rules_apply_in_order_and_disabled_rules_are_skipped(env):
    add_rule(env, name="off", conditions=[{"type": "extension", "values": ["jpg"]}], action="aside", enabled=False)
    add_rule(env, name="on", conditions=[{"type": "extension", "values": ["jpg"]}], action="review")
    env.add("a.JPG", photo(*SUNDAY))
    env.settle(NOW)
    assert env.tree(env.root / "_a_revoir") == ["regle_on/a.JPG"]


def test_sidecar_files_are_reviewed_unless_a_rule_takes_them(env):
    env.add("IMG_1.AAE", None)
    env.settle(NOW)
    assert env.tree(env.root / "_a_revoir") == ["no_date/IMG_1.AAE"]
    template = next(t for t in rules.TEMPLATES if t["id"] == "sidecars")
    add_rule(env, **template)
    env.add("IMG_2.AAE", None)
    env.settle(NOW + 3 * 24 * 3600)
    assert env.tree(env.root / "_mis_de_cote") == ["fichiers_annexes_aae_thm/IMG_2.AAE"]


def test_rule_validation():
    with pytest.raises(ValueError):
        rules.validate({"name": "x", "conditions": [], "action": "aside"})
    with pytest.raises(ValueError):
        rules.validate({"name": "x", "conditions": [{"type": "bogus"}], "action": "aside"})
    with pytest.raises(ValueError):
        rules.validate({"name": "x", "conditions": [{"type": "media", "value": "photo"}], "action": "delete"})
    with pytest.raises(ValueError):
        rules.validate({"name": "x", "conditions": [{"type": "media", "value": "photo"}], "action": "route",
                        "params": {"folder": "../out"}})  # fmt: skip
    with pytest.raises(ValueError):
        rules.validate({"name": "x", "conditions": [{"type": "hours", "window": "25:00-26:00"}], "action": "aside"})


def test_preview_counts_inbox_files(env):
    env.add("Screenshot_1.PNG", photo(*SUNDAY))
    env.add("Screenshot_2.PNG", photo(*SUNDAY))
    env.add("IMG_1.JPG", photo(*SUNDAY))
    env.run(NOW)  # reads the metadata
    conn = env.conn()
    cfg = namespace(load(conn))
    out = rules.preview(conn, cfg, env.inbox, [{"type": "filename", "patterns": ["screenshot*"]}])
    assert out["count"] == 2 and out["total"] == 3 and out["examples"][0].startswith("Screenshot")
    conn.close()


def test_rule_versions_can_be_restored(env):
    conn = env.conn()
    rules.create(conn, {"name": "a", "conditions": [{"type": "media", "value": "video"}], "action": "review"})
    v1 = rules.snapshot(conn, "first")
    rules.create(conn, {"name": "b", "conditions": [{"type": "media", "value": "photo"}], "action": "review"})
    rules.snapshot(conn, "second")
    assert len(rules.load(conn)) == 2
    assert rules.restore(conn, v1) and [r["name"] for r in rules.load(conn)] == ["a"]
    conn.close()


# -- zones -------------------------------------------------------------------------------------
def test_work_zone_sets_aside_on_weekdays_only(env):
    add_zone(env, name="Bureau", type="work", weekdays=[0, 1, 2, 3, 4])
    env.add("mon.JPG", photo(*MONDAY, gps=OFFICE))
    env.add("sun.JPG", photo(*SUNDAY, gps=OFFICE))
    job = env.settle(NOW)
    assert env.tree(env.root / "_mis_de_cote") == ["zone_bureau/mon.JPG"]
    assert env.tree(env.library) == ["2026/2026.03 Vie de famille/sun.JPG"]
    assert "Zone « Bureau »" in journal(env, job["id"])[0]["why"]


def test_zone_time_window(env):
    add_zone(env, name="Bureau", type="work", time_window="09:00-17:00")
    env.add("noon.JPG", photo(*MONDAY, hour=12, gps=OFFICE))
    env.add("night.JPG", photo(*MONDAY, hour=21, gps=OFFICE))
    env.settle(NOW)
    assert env.tree(env.root / "_mis_de_cote") == ["zone_bureau/noon.JPG"]


def test_volume_exception_sends_a_big_day_to_review(env):
    add_zone(env, name="Bureau", type="work", volume_exception=3)
    for i in range(3):
        env.add(f"big{i}.JPG", photo(*MONDAY, hour=9 + i, gps=OFFICE))
    env.add("small.JPG", photo(2026, 3, 3, gps=OFFICE))
    env.settle(NOW)
    assert env.tree(env.root / "_a_revoir") == [f"zone_bureau/big{i}.JPG" for i in range(3)]
    assert env.tree(env.root / "_mis_de_cote") == ["zone_bureau/small.JPG"]


def test_exclusion_zone_with_leave_action_keeps_files_in_the_inbox(env):
    add_zone(env, name="Prive", type="exclusion", action="leave")
    env.add("a.JPG", photo(*SUNDAY, gps=OFFICE))
    job = env.settle(NOW)
    assert (env.inbox / "a.JPG").exists() and job["stats"]["held"] == {"zone_leave": 1}


def test_frequent_zone_names_the_place_and_overrides_the_threshold(env):
    add_zone(env, name="Chez mamie", type="frequent", place_name="Chez mamie", event_min=2, label="Visite",
             geometry={"lat": OFFICE[0], "lon": OFFICE[1], "radius_m": 500})  # fmt: skip
    env.settings(event_min_items=10, away_event_min_items=10)
    env.add("a.JPG", photo(*SUNDAY, hour=10, gps=OFFICE))
    env.add("b.JPG", photo(*SUNDAY, hour=11, gps=OFFICE))
    env.settle(NOW)
    assert env.tree(env.library) == ["2026/2026.03.01 Visite, Chez mamie/a.JPG", "2026/2026.03.01 Visite, Chez mamie/b.JPG"]


def test_polygon_zone():
    square = {"name": "p", "type": "work", "shape": "polygon",
              "geometry": {"points": [[48.0, 2.0], [48.0, 3.0], [49.0, 3.0], [49.0, 2.0]]}}  # fmt: skip
    zone = zones.validate(square)
    assert zones.contains(zone, (48.5, 2.5)) and not zones.contains(zone, (50.0, 2.5))


def test_zone_validation():
    with pytest.raises(ValueError):
        zones.validate({"name": "", "type": "work", "geometry": {"lat": 1, "lon": 1, "radius_m": 5}})
    with pytest.raises(ValueError):
        zones.validate({"name": "z", "type": "nope", "geometry": {"lat": 1, "lon": 1, "radius_m": 5}})
    with pytest.raises(ValueError):
        zones.validate({"name": "z", "type": "work", "geometry": {"lat": 95, "lon": 1, "radius_m": 5}})


def test_restoring_from_aside_exempts_the_file_from_the_rule(env):
    add_rule(env, name="r", conditions=[{"type": "extension", "values": ["jpg"]}], action="aside")
    env.add("a.JPG", photo(*SUNDAY))
    env.settle(NOW)
    conn = env.conn()
    paths = resolve_paths(env.root, load(conn))
    items = maintenance.list_aside(conn, paths)
    assert [i["name"] for i in items] == ["a.JPG"] and "Règle « r »" in items[0]["why"]
    assert maintenance.restore_aside(conn, paths, [items[0]["id"]])["restored"] == 1
    conn.close()
    assert (env.inbox / "a.JPG").exists()
    env.settle(NOW + 3 * 24 * 3600)
    assert env.tree(env.library) == ["2026/2026.03 Vie de famille/a.JPG"]


def test_purge_is_opt_in_and_needs_confirmation(env):
    add_rule(env, name="r", conditions=[{"type": "extension", "values": ["jpg"]}], action="aside")
    env.add("a.JPG", photo(*SUNDAY))
    env.settle(NOW)
    later = NOW + 40 * 24 * 3600
    env.settings(aside_purge_days=30)  # not confirmed: nothing happens
    env.run(later)
    assert len(env.tree(env.root / "_mis_de_cote")) == 1
    env.settings(aside_purge_confirmed=True)
    job = env.run(later + 3600)
    assert env.tree(env.root / "_mis_de_cote") == [] and job["stats"]["purged"] == 1


# -- validation mode, approvals, locks ---------------------------------------------------------
def fill_event(env, day=(2026, 3, 14), n=4, prefix="E"):
    for i in range(n):
        env.add(f"{prefix}{i}.JPG", photo(*day, hour=10 + i))


def test_validation_mode_proposes_instead_of_creating(env):
    env.settings(validation_mode=True)
    fill_event(env)
    job = env.settle(NOW)
    assert job["stats"]["held"] == {"awaiting_validation": 4} and env.tree(env.library) == []
    conn = env.conn()
    row = conn.execute("SELECT * FROM proposal").fetchone()
    assert (row["start_date"], row["end_date"], row["count"], row["place"]) == ("2026-03-14", "2026-03-14", 4, "Testville")
    conn.close()


def test_approval_names_the_event(env):
    env.settings(validation_mode=True)
    fill_event(env)
    env.settle(NOW)
    conn = env.conn()
    conn.execute("INSERT INTO approval(start_date, end_date, action, label, place, created_at) VALUES(?,?,?,?,?,?)",
                 ("2026-03-14", "2026-03-14", "event", "Anniversaire", "Provins", time.time()))  # fmt: skip
    conn.commit()
    conn.close()
    env.run(NOW + 60)
    assert (env.library / "2026" / "2026.03.14 Anniversaire, Provins").is_dir()


def test_approval_can_merge_two_days_into_one_stay(env):
    env.settings(validation_mode=True, max_gap_days_in_stay=0)
    fill_event(env, (2026, 3, 14), prefix="D14_")
    fill_event(env, (2026, 3, 16), prefix="D16_")
    env.settle(NOW)
    conn = env.conn()
    assert conn.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 2
    conn.execute("INSERT INTO approval(start_date, end_date, action, label, place, created_at) VALUES(?,?,?,?,?,?)",
                 ("2026-03-14", "2026-03-16", "event", "Week-end", "Provins", time.time()))  # fmt: skip
    conn.commit()
    conn.close()
    env.run(NOW + 60)
    assert (env.library / "2026" / "2026.03.14~16 Week-end, Provins").is_dir()


def test_approval_as_routine_keeps_the_day_in_the_monthly_folder(env):
    env.settings(validation_mode=True)
    fill_event(env)
    env.settle(NOW)
    conn = env.conn()
    conn.execute("INSERT INTO approval(start_date, end_date, action, created_at) VALUES(?,?,?,?)",
                 ("2026-03-14", "2026-03-14", "routine", time.time()))  # fmt: skip
    conn.commit()
    conn.close()
    env.run(NOW + 60)
    assert len(env.tree(env.library / "2026" / "2026.03 Vie de famille")) == 4


def test_locked_folder_receives_nothing_new(env):
    env.add("a.JPG", photo(*SUNDAY))
    env.settle(NOW)
    conn = env.conn()
    conn.execute("UPDATE folder SET status='verrouille'")
    conn.commit()
    conn.close()
    env.add("b.JPG", photo(2026, 3, 2))
    env.settle(NOW + 3 * 24 * 3600)
    assert env.tree(env.root / "_a_revoir") == ["folder_locked/b.JPG"]


# -- naming templates --------------------------------------------------------------------------
def test_custom_templates_round_trip(env):
    tpl = "{range} - {label} ({place})"
    naming.validate_template("event", tpl)
    name = naming.event_name(*(naming.date(2026, 3, 14),) * 2, "Mariage", "Lyon", tpl)
    assert name == "2026.03.14 - Mariage (Lyon)"
    parsed = naming.parse_name(name, "Vie", tpl)
    assert (parsed.label, parsed.place, parsed.role) == ("Mariage", "Lyon", "evenement")


def test_template_validation():
    for kind, bad in (("event", "{label}"), ("event", "{range} {nope}"), ("event", "{range}/{label}"),
                      ("routine", "{year} {label}"), ("event", "{range} {place} {label}")):  # fmt: skip
        with pytest.raises(ValueError):
            naming.validate_template(kind, bad)


def test_default_event_names_still_parse_with_commas_and_without_place():
    p = naming.parse_name("2026.03.14 Anniversaire, Mamie, Provins", "Vie")
    assert (p.label, p.place) == ("Anniversaire, Mamie", "Provins")
    q = naming.parse_name("2026.03.14~16 Week-end", "Vie")
    assert (q.label, q.place, q.role) == ("Week-end", None, "sejour")


def test_custom_template_is_used_for_new_folders(env):
    env.settings(event_template="{range} - {label} ({place})", routine_template="{year}-{month} {label}")
    fill_event(env)
    env.add("x.JPG", photo(2026, 3, 2))
    env.settle(NOW)
    assert (env.library / "2026" / "2026.03.14 - À nommer (Testville)").is_dir()
    assert (env.library / "2026" / "2026-03 Vie de famille" / "x.JPG").exists()


# -- content analysis --------------------------------------------------------------------------
PIL = pytest.importorskip("PIL")


def scene():
    """A synthetic picture with sharp edges, a gradient and fine lines (no symmetry)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 240))
    draw = ImageDraw.Draw(img)
    for x in range(320):
        shade = 40 + int(180 * x / 320)
        draw.line([(x, 0), (x, 239)], fill=(shade, shade, shade))
    draw.rectangle([20, 20, 120, 100], fill=(245, 245, 245))
    draw.rectangle([200, 120, 300, 220], fill=(10, 10, 10))
    for k in range(0, 200, 6):
        draw.line([(160 + k // 2, 10), (130 + k // 2, 110)], fill=(0, 0, 0), width=1)
    return img


def image_bytes(kind: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    if kind == "sharp":
        img = scene()
    elif kind == "blurry":
        img = scene().filter(ImageFilter.GaussianBlur(6))
    elif kind == "dark":
        img = Image.new("RGB", (320, 240), (4, 4, 6))
    else:  # paper
        img = Image.new("RGB", (320, 240), (250, 250, 250))
        draw = ImageDraw.Draw(img)
        for y in range(10, 230, 14):
            draw.rectangle([20, y, 300, y + 4], fill=(30, 30, 30))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


@pytest.mark.parametrize("kind,flag", [("blurry", "blur"), ("dark", "dark"), ("paper", "document")])
def test_analysis_flags(env, kind, flag):
    from synopixtri import analysis

    path = env.add("a.JPG", photo(*SUNDAY), content=image_bytes(kind))
    data = analysis.analyze(path)
    cfg = namespace(load(env.conn()))
    assert flag in analysis.flags("a.JPG", photo(*SUNDAY), data, cfg)
    sharp = analysis.analyze(env.add("s.JPG", photo(*SUNDAY), content=image_bytes("sharp")))
    assert "blur" not in analysis.flags("s.JPG", photo(*SUNDAY), sharp, cfg)


def test_blur_rule_sends_only_blurry_photos_to_review(env):
    template = next(t for t in rules.TEMPLATES if t["id"] == "blurry")
    add_rule(env, **template)
    env.add("blur.JPG", photo(*SUNDAY), content=image_bytes("blurry"))
    env.add("ok.JPG", photo(*SUNDAY, hour=11), content=image_bytes("sharp"))
    job = env.settle(NOW)
    assert env.tree(env.root / "_a_revoir") == ["regle_photos_floues/blur.JPG"]
    assert job["stats"]["analysed"] >= 1


def test_without_an_analysis_rule_nothing_is_analysed(env):
    env.add("a.JPG", photo(*SUNDAY), content=image_bytes("blurry"))
    job = env.settle(NOW)
    assert job["stats"]["analysed"] == 0 and len(env.tree(env.library)) == 1


def test_near_duplicates_keep_the_sharpest(env):
    from PIL import Image, ImageOps

    from synopixtri import analysis

    sharp = image_bytes("sharp")
    soft = io.BytesIO()
    Image.open(io.BytesIO(sharp)).save(soft, "JPEG", quality=30)
    other = io.BytesIO()
    ImageOps.mirror(scene()).save(other, "JPEG")
    a = analysis.analyze(env.add("a.JPG", photo(*SUNDAY), content=sharp))
    b = analysis.analyze(env.add("b.JPG", photo(*SUNDAY), content=soft.getvalue()))
    c = analysis.analyze(env.add("c.JPG", photo(*SUNDAY), content=other.getvalue()))
    flagged, groups = analysis.near_duplicates([("a", a), ("b", b), ("c", c)], 4)
    assert len(groups) == 1 and set(groups[0]) == {"a", "b"} and groups[0][0] == "a" and flagged == {"b"}
    assert os.path.exists(env.inbox / "a.JPG")


def test_metadata_is_read_in_saved_chunks_and_stops_at_the_time_budget(env):
    from pathlib import Path
    from types import SimpleNamespace

    from synopixtri import jobs
    from synopixtri.models import Meta

    items = [SimpleNamespace(path=Path(f"/inbox/{i}.jpg"), meta=None, ext=".jpg", stable=True) for i in range(450)]
    conn = env.conn()
    for item in items:
        conn.execute("INSERT INTO inbox_file(path,size,mtime_ns,first_seen,last_change) VALUES(?,1,1,0,0)", (str(item.path),))
    calls = []

    def reader(paths):
        calls.append(len(paths))
        return {p: Meta(mime="image/jpeg") for p in paths}

    jobs._read_metadata(conn, items, reader, budget_s=3600)
    assert calls == [200, 200, 50] and all(i.meta for i in items)

    fresh = [SimpleNamespace(path=Path(f"/inbox/{i}.jpg"), meta=None, ext=".jpg", stable=True) for i in range(450)]
    calls.clear()
    jobs._read_metadata(conn, fresh, reader, budget_s=-1)  # budget already spent: one chunk, the rest later
    assert calls == [200] and sum(1 for i in fresh if i.meta) == 200
    conn.close()


def test_dates_from_file_names_and_file_dates():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from synopixtri import exif
    from synopixtri.models import Meta

    now = datetime(2026, 10, 7, 12).timestamp()
    tz = ZoneInfo("UTC")
    named = lambda n: exif.date_from_name(n, datetime(2026, 10, 7))  # noqa: E731
    assert named("Resized_20231013_120133_1.jpeg") == datetime(2023, 10, 13, 12, 1, 33)
    assert named("IMG-20240229-WA0001.jpg") == datetime(2024, 2, 29)
    assert named("2023-10-13 12.01.33.jpg") == datetime(2023, 10, 13, 12, 1, 33)
    assert named("IMG_6849.JPG") is None and named("20231313_000000.jpg") is None
    assert named("88d6c274-d213-454f-98ec-84ad7e512680.jpg") is None and named("20990101_000000.jpg") is None

    def fresh(**kw):
        return Meta(local_dt=datetime(2019, 5, 1), date_source="file", date_reliable=False, **kw)

    old_file = fresh()  # file date 40 days before the arrival: believed in auto mode
    exif.refine_date(old_file, "IMG_1.JPG", int((now - 40 * 86400) * 1e9), now, "auto", 2, now, tz)
    assert old_file.date_reliable
    copied = fresh()  # file date = arrival: it is the copy date
    exif.refine_date(copied, "IMG_1.JPG", int(now * 1e9), now, "auto", 2, now, tz)
    assert not copied.date_reliable
    exif.refine_date(copied, "IMG_1.JPG", int(now * 1e9), now, "always", 2, now, tz)
    assert copied.date_reliable
    never = fresh()
    exif.refine_date(never, "20231013_120133.jpg", 0, now, "never", 2, now, tz)
    assert not never.date_reliable
