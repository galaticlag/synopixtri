from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from synopixtri import exif, naming

TZ = ZoneInfo("Europe/Paris")


def test_photo_uses_exif_local_date():
    meta = exif.parse_record(
        Path("a.jpg"),
        {"DateTimeOriginal": "2026:01:02 11:13:37", "OffsetTimeOriginal": "+01:00", "GPSLatitude": 48.85, "GPSLongitude": 2.35},
        TZ,
    )
    assert meta.local_dt.isoformat() == "2026-01-02T11:13:37"
    assert meta.date_reliable and meta.lat == 48.85


def test_video_prefers_keys_creation_date_with_offset():
    meta = exif.parse_record(
        Path("a.mov"),
        {"CreationDate": "2026:09:20 15:16:53+02:00", "CreateDate": "2026:09:20 13:16:53"},
        TZ,
    )
    assert meta.local_dt.isoformat() == "2026-09-20T15:16:53"
    assert meta.date_source == "keys_local"


def test_video_utc_date_is_converted_with_daylight_saving():
    summer = exif.parse_record(Path("a.mov"), {"CreateDate": "2026:09:20 13:16:53"}, TZ)
    winter = exif.parse_record(Path("a.mov"), {"CreateDate": "2026:01:02 10:13:36"}, TZ)
    assert summer.local_dt.hour == 15 and winter.local_dt.hour == 11


def test_utc_conversion_can_change_the_day():
    meta = exif.parse_record(Path("a.mov"), {"CreateDate": "2026:06:30 23:30:00"}, TZ)
    assert meta.local_dt.date() == date(2026, 7, 1)


def test_file_date_is_a_last_resort_and_unreliable():
    meta = exif.parse_record(Path("a.png"), {"FileModifyDate": "2026:10:07 11:18:38+02:00"}, TZ)
    assert meta.date_source == "file" and not meta.date_reliable


def test_zero_dates_and_zero_gps_are_ignored():
    meta = exif.parse_record(
        Path("a.mov"), {"CreateDate": "0000:00:00 00:00:00", "GPSLatitude": 0, "GPSLongitude": 0}, TZ
    )
    assert meta.local_dt is None and meta.lat is None


def test_exiftool_error_is_kept():
    assert exif.parse_record(Path("a.jpg"), {"Error": "File format error"}, TZ).error


def test_format_range():
    assert naming.format_range(date(2021, 1, 14), date(2021, 1, 14)) == "2021.01.14"
    assert naming.format_range(date(2021, 1, 13), date(2021, 1, 16)) == "2021.01.13~16"
    assert naming.format_range(date(2021, 7, 21), date(2021, 8, 10)) == "2021.07.21~08.10"
    assert naming.format_range(date(2024, 12, 28), date(2025, 1, 3)) == "2024.12.28~2025.01.03"


def test_parse_name_round_trip():
    for start, end in [
        (date(2021, 1, 14), date(2021, 1, 14)),
        (date(2021, 1, 13), date(2021, 1, 16)),
        (date(2021, 7, 21), date(2021, 8, 10)),
        (date(2024, 12, 28), date(2025, 1, 3)),
    ]:
        parsed = naming.parse_name(naming.event_name(start, end, "Fête", "Lille"), "Vie de famille")
        assert (parsed.date_start, parsed.date_end, parsed.label, parsed.place) == (start, end, "Fête", "Lille")


def test_parse_routine_and_free_folders():
    routine = naming.parse_name("2021.01 Vie de famille", "Vie de famille")
    assert routine.role == "routine_mois" and routine.month_key == "2021-01"
    assert naming.parse_name("2021.01 Vacances ski", "Vie de famille") is None
    assert naming.parse_name("Divers", "Vie de famille") is None


def test_sanitize_removes_illegal_characters():
    assert "/" not in naming.sanitize("a/b:c") and naming.sanitize("  x.  ") == "x"
