from pathlib import Path
from types import SimpleNamespace

from synopixtri import units
from synopixtri.models import Item

from conftest import photo, video

CFG = SimpleNamespace(live_partner_wait_min=60)
NOW = 1_000_000.0


def item(name, meta, *, stable=True, age=7200.0, folder="inbox"):
    return Item(Path(folder) / name, 10, 0, NOW - age, NOW - age, stable, meta)


def kinds(items, now=NOW):
    ready, held = units.build_units(items, CFG, now)
    return sorted(u.kind for u in ready), sorted(r for _u, r in held)


def test_pair_by_name_and_content_identifier():
    ready, _ = units.build_units(
        [item("IMG_1.JPG", photo(2026, 1, 2, cid="A")), item("IMG_1.MOV", video(2026, 1, 2, cid="A"))], CFG, NOW
    )
    assert [u.kind for u in ready] == ["live_pair"] and ready[0].live.path.name == "IMG_1.MOV"


def test_pair_by_identifier_even_with_different_names():
    ready, _ = units.build_units(
        [item("IMG_1.JPG", photo(2026, 1, 2, cid="A")), item("IMG_E1.MOV", video(2026, 1, 2, cid="A"))], CFG, NOW
    )
    assert [u.kind for u in ready] == ["live_pair"]


def test_same_name_but_different_identifiers_do_not_pair():
    ready_kinds, _ = kinds(
        [item("IMG_1.JPG", photo(2026, 1, 2, cid="A")), item("IMG_1.MOV", video(2026, 1, 2, cid="B"))]
    )
    assert "live_pair" not in ready_kinds


def test_name_pairing_needs_a_short_video_when_no_identifier():
    short = [item("a.JPG", photo(2026, 1, 2)), item("a.MOV", video(2026, 1, 2, duration=2.0))]
    long = [item("b.JPG", photo(2026, 1, 2)), item("b.MOV", video(2026, 1, 2, duration=60.0))]
    assert kinds(short)[0] == ["live_pair"]
    assert kinds(long)[0] == ["photo_single", "video_single"]


def test_lone_live_video_waits_for_its_photo_then_becomes_orphan():
    mov = item("IMG_9.MOV", video(2026, 1, 2, cid="Z"), age=600)
    assert kinds([mov]) == ([], ["waiting_live_partner"])
    old = item("IMG_9.MOV", video(2026, 1, 2, cid="Z"), age=7200)
    assert kinds([old]) == (["orphan_live_mov"], [])


def test_photo_with_identifier_but_no_video_waits_then_is_single():
    recent = item("IMG_5.JPG", photo(2026, 1, 2, cid="Q"), age=600)
    assert kinds([recent]) == ([], ["waiting_live_partner"])
    old = item("IMG_5.JPG", photo(2026, 1, 2, cid="Q"), age=7200)
    assert kinds([old]) == (["photo_single"], [])


def test_stable_file_waits_while_its_namesake_is_still_uploading():
    items = [item("IMG_1.JPG", photo(2026, 1, 2, cid="A")), item("IMG_1.MOV", None, stable=False)]
    assert kinds(items) == ([], ["partner_uploading"])


def test_screenshot_and_plain_video_are_single_units():
    items = [item("shot.PNG", photo(2026, 1, 2, gps=None)), item("clip.MP4", video(2026, 1, 2, duration=40))]
    assert kinds(items)[0] == ["photo_single", "video_single"]
