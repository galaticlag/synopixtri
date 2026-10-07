from __future__ import annotations

import os

from synopixtri import jobs

from conftest import PARIS, photo, ts, video

NOW = ts(2026, 3, 20)


def test_stability_delay_nothing_moves_on_first_sighting(env):
    env.add("a.JPG", photo(2026, 3, 1))
    job = env.run(NOW)  # first sighting: not stable yet
    assert job["stats"]["stable_files"] == 0 and (env.inbox / "a.JPG").exists()
    job = env.run(NOW + 11 * 60)  # unchanged for more than 10 minutes
    assert job["stats"]["moved"] == 1 and not (env.inbox / "a.JPG").exists()


def test_growing_file_is_not_touched(env):
    path = env.add("a.JPG", photo(2026, 3, 1))
    env.run(NOW)
    path.write_bytes(path.read_bytes() + b"more data")  # still being uploaded
    job = env.run(NOW + 11 * 60)
    assert job["stats"]["stable_files"] == 0 and path.exists()


def test_routine_goes_to_monthly_folder_and_live_to_subfolder(env):
    env.add("IMG_1.JPG", photo(2026, 3, 1, cid="A"))
    env.add("IMG_1.MOV", video(2026, 3, 1, cid="A"))
    env.add("IMG_2.JPG", photo(2026, 3, 2))
    env.settle(NOW)
    base = "library/2026/2026.03 Vie de famille"
    assert env.tree() == sorted([
        f"{base}/IMG_1.JPG", f"{base}/IMG_2.JPG", f"{base}/2026.03 Vie de famille - live/IMG_1.MOV",
    ])  # fmt: skip


def test_event_needs_maturity_then_gets_provisional_name(env):
    for i in range(4):
        env.add(f"E{i}.JPG", photo(2026, 3, 14, 10 + i))
    env.run(NOW - 3600)  # first sighting 1 hour ago
    job = env.run(NOW)  # stable, but quiet for less than 48 h
    assert job["stats"]["held"] == {"maturing": 4} and len(env.tree(env.inbox)) == 4
    job = env.settle(NOW + 4 * 24 * 3600)
    assert job["stats"]["moved"] == 4
    assert (env.library / "2026" / "2026.03.14 À nommer, Testville").is_dir()


def test_small_day_stays_routine_but_big_day_becomes_event_with_promotion(env):
    env.add("a.JPG", photo(2026, 3, 14, 9))
    env.add("b.JPG", photo(2026, 3, 14, 10))
    env.settle(NOW)
    assert (env.library / "2026" / "2026.03 Vie de famille" / "a.JPG").exists()
    # More photos of the same day arrive later: the day now qualifies as an event.
    env.add("c.JPG", photo(2026, 3, 14, 11))
    env.add("d.JPG", photo(2026, 3, 14, 12))
    env.settle(NOW + 10 * 24 * 3600)
    event = env.library / "2026" / "2026.03.14 À nommer, Testville"
    assert sorted(p.name for p in event.iterdir()) == ["a.JPG", "b.JPG", "c.JPG", "d.JPG"]
    assert not any(env.library.rglob("2026.03 Vie de famille/*.JPG"))


def test_multi_day_stay_merges_consecutive_days(env):
    for day in (14, 15, 16):
        for i in range(3):
            env.add(f"D{day}_{i}.JPG", photo(2026, 3, day, 9 + i))
    env.settle(NOW)
    assert (env.library / "2026" / "2026.03.14~16 À nommer, Testville").is_dir()


def test_user_rename_is_detected_and_respected(env):
    for i in range(4):
        env.add(f"E{i}.JPG", photo(2026, 3, 14, 10 + i))
    env.settle(NOW)
    original = env.library / "2026" / "2026.03.14 À nommer, Testville"
    renamed = env.library / "2026" / "2026.03.14 Anniversaire, Provins"
    os.rename(original, renamed)
    # A late photo of the same day arrives, then a pass runs.
    env.add("late.JPG", photo(2026, 3, 14, 18))
    env.settle(NOW + 5 * 24 * 3600)  # the first of the two passes notices the rename
    assert (renamed / "late.JPG").exists() and not original.exists()
    conn = env.conn()
    row = conn.execute("SELECT status FROM folder WHERE path=?", (str(renamed),)).fetchone()
    assert row["status"] == "utilisateur"
    # Folders renamed by the user no longer count as "to name".
    from synopixtri import folders

    assert folders.to_name(conn, "À nommer") == []


def test_managed_folder_is_widened_when_the_stay_continues(env):
    for i in range(3):
        env.add(f"A{i}.JPG", photo(2026, 3, 14, 9 + i))
    env.settle(NOW)
    assert (env.library / "2026" / "2026.03.14 À nommer, Testville").is_dir()
    for i in range(3):
        env.add(f"B{i}.JPG", photo(2026, 3, 15, 9 + i))
    env.settle(NOW + 6 * 24 * 3600)
    stay = env.library / "2026" / "2026.03.14~15 À nommer, Testville"
    assert stay.is_dir() and len(list(stay.iterdir())) == 6
    assert not (env.library / "2026" / "2026.03.14 À nommer, Testville").exists()


def test_duplicates_go_to_quarantine(env):
    env.add("a.JPG", photo(2026, 3, 1), content=b"same bytes")
    env.add("b.JPG", photo(2026, 3, 1, 11), content=b"same bytes")
    env.settle(NOW)
    assert len(env.tree(env.library)) == 1
    assert len(env.tree(env.root / "_doublons")) == 1


def test_duplicate_of_a_file_already_in_the_library(env):
    env.add("a.JPG", photo(2026, 3, 1), content=b"same bytes")
    env.settle(NOW)
    env.add("copy.JPG", photo(2026, 3, 1), content=b"same bytes")
    env.settle(NOW + 24 * 3600)
    assert len(env.tree(env.root / "_doublons")) == 1 and len(env.tree(env.library)) == 1


def test_unreliable_date_goes_to_review(env):
    env.settings(file_date_policy="never")
    meta = photo(2026, 3, 1)
    meta.date_reliable, meta.date_source = False, "file"
    env.add("nodate.PNG", meta)
    env.add("broken.JPG", None)
    env.settle(NOW)
    assert env.tree(env.root / "_a_revoir") == ["exif_error/broken.JPG", "no_date/nodate.PNG"]


def test_orphan_live_video_follows_policy(env):
    env.add("IMG_3.MOV", video(2026, 3, 1, cid="Z"))
    env.settle(NOW)
    assert env.tree(env.library)[0].endswith("2026.03 Vie de famille - live/IMG_3.MOV")


def test_undo_restores_everything(env):
    for i in range(4):
        env.add(f"E{i}.JPG", photo(2026, 3, 14, 10 + i))
    env.add("IMG_1.JPG", photo(2026, 3, 2, cid="A"))
    env.add("IMG_1.MOV", video(2026, 3, 2, cid="A"))
    before = env.tree(env.inbox)
    job = env.settle(NOW)
    assert env.tree(env.inbox) == []
    result = jobs.undo(env.boot, job["id"])
    assert result["state"] == "undone" and result["restored"] == 6
    assert env.tree(env.inbox) == before
    assert env.tree(env.library) == []
    assert not any(p.is_dir() for p in env.library.rglob("*"))  # created folders are removed
    conn = env.conn()
    assert conn.execute("SELECT COUNT(*) FROM placed_file").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM folder").fetchone()[0] == 0


def test_undo_reports_conflicts_instead_of_overwriting(env):
    env.add("a.JPG", photo(2026, 3, 1))
    job = env.settle(NOW)
    (env.inbox / "a.JPG").write_bytes(b"someone put another file here")
    result = jobs.undo(env.boot, job["id"])
    assert result["state"] == "partial_undo" and result["conflicts"]
    assert (env.inbox / "a.JPG").read_bytes() == b"someone put another file here"


def test_emergency_brake_pauses_then_confirm_runs(env):
    env.settings(brake_max_files=2)
    for i in range(3):
        env.add(f"a{i}.JPG", photo(2026, 3, 1 + i))
    job = env.settle(NOW)
    assert job["state"] == "paused_brake" and len(env.tree(env.inbox)) == 3
    job = env.run(NOW + 60, skip_brake=True)
    assert job["state"] == "done" and env.tree(env.inbox) == []


def test_dry_run_changes_nothing_but_reports(env):
    env.add("a.JPG", photo(2026, 3, 1))
    env.run(NOW - 3 * 24 * 3600)
    job = env.run(NOW, dry_run=True)
    assert job["state"] == "dry_run" and job["stats"]["planned"] == 1
    assert (env.inbox / "a.JPG").exists() and env.tree(env.library) == []


def test_late_photo_attaches_to_existing_event_without_threshold(env):
    for i in range(3):
        env.add(f"E{i}.JPG", photo(2026, 3, 14, 10 + i))
    env.settle(NOW)
    env.add("late.JPG", photo(2026, 3, 14, 20))
    env.settle(NOW + 30 * 24 * 3600)
    event = env.library / "2026" / "2026.03.14 À nommer, Testville"
    assert (event / "late.JPG").exists()


def test_existing_user_folder_is_adopted(env):
    mine = env.library / "2026" / "2026.03.14 Mariage, Lyon"
    mine.mkdir(parents=True)
    (mine / "old.JPG").write_bytes(b"x")
    env.add("new.JPG", photo(2026, 3, 14, 16))
    env.settle(NOW)
    conn = env.conn()
    row = conn.execute("SELECT status, role FROM folder WHERE path=?", (str(mine),)).fetchone()
    assert (row["status"], row["role"]) == ("utilisateur", "evenement")
    assert (mine / "new.JPG").exists() and not any(env.library.glob("2026/2026.03.14 À nommer*"))


def test_away_threshold_uses_home_position(env):
    env.settings(home_lat=PARIS[0], home_lon=PARIS[1], home_radius_km=50.0, event_min_items=10, away_event_min_items=2)
    far = (45.76, 4.84)
    env.add("away1.JPG", photo(2026, 3, 14, 10, gps=far))
    env.add("away2.JPG", photo(2026, 3, 14, 11, gps=far))
    env.add("home1.JPG", photo(2026, 3, 15, 10))
    env.add("home2.JPG", photo(2026, 3, 15, 11))
    env.settle(NOW)
    names = sorted(p.name for p in (env.library / "2026").iterdir())
    assert names == ["2026.03 Vie de famille", "2026.03.14 À nommer, Testville"]
