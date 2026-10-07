"""SQLite storage: schema and connections."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_file (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    first_seen  REAL NOT NULL,
    last_change REAL NOT NULL,
    meta_json   TEXT
);

CREATE TABLE IF NOT EXISTS job (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL,
    finished_at REAL,
    stats_json  TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS operation_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    INTEGER NOT NULL REFERENCES job(id),
    seq       INTEGER NOT NULL,
    op        TEXT NOT NULL,          -- move | mkdir | rename_dir
    src       TEXT,
    dst       TEXT,
    state     TEXT NOT NULL,          -- planned | pending | done | failed | undone | undo_failed
    reason    TEXT,
    unit_key  TEXT,
    meta_json TEXT,
    error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_oplog_job ON operation_log(job_id, seq);

CREATE TABLE IF NOT EXISTS folder (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    path           TEXT NOT NULL UNIQUE,
    role           TEXT NOT NULL,     -- routine_mois | evenement | sejour | libre
    status         TEXT NOT NULL,     -- gere | utilisateur | verrouille
    managed_name   TEXT,              -- name given by the app, NULL for user folders
    date_start     TEXT,
    date_end       TEXT,
    lat            REAL,
    lon            REAL,
    month_key      TEXT,
    created_job_id INTEGER,
    missing        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_folder_month ON folder(month_key);

CREATE TABLE IF NOT EXISTS placed_file (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id  INTEGER NOT NULL REFERENCES folder(id),
    rel_path   TEXT NOT NULL,         -- relative to the folder
    dev        INTEGER,
    ino        INTEGER,
    size       INTEGER,
    mtime_ns   INTEGER,
    ref_date   TEXT,
    unit_key   TEXT,
    is_live    INTEGER NOT NULL DEFAULT 0,
    content_id TEXT,
    md5        TEXT,
    job_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_placed_date ON placed_file(ref_date);
CREATE INDEX IF NOT EXISTS idx_placed_size ON placed_file(size);
CREATE INDEX IF NOT EXISTS idx_placed_folder ON placed_file(folder_id);

CREATE TABLE IF NOT EXISTS geocode_cache (
    key  TEXT PRIMARY KEY,
    city TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zone (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    type             TEXT NOT NULL,   -- home | work | frequent | exclusion
    shape            TEXT NOT NULL,   -- circle | polygon
    geometry_json    TEXT NOT NULL,
    action           TEXT NOT NULL DEFAULT 'aside',   -- aside | leave | review (work, exclusion)
    weekdays_json    TEXT,            -- [0..6], Monday = 0; NULL = every day
    time_window      TEXT,            -- "HH:MM-HH:MM"; NULL = all day
    volume_exception INTEGER,         -- N or more media the same day: review instead of the action
    event_min        INTEGER,         -- overrides the event threshold (0 = never an event)
    place_name       TEXT,            -- replaces the geocoded place in folder names
    label            TEXT,            -- replaces the provisional label for events here
    enabled          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    position        INTEGER NOT NULL DEFAULT 0,
    conditions_json TEXT NOT NULL,
    action          TEXT NOT NULL,    -- aside | ignore | review | route
    params_json     TEXT
);

CREATE TABLE IF NOT EXISTS approval (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    action     TEXT NOT NULL DEFAULT 'event',   -- event | routine
    label      TEXT,
    place      TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    lat        REAL,
    lon        REAL,
    place      TEXT,
    count      INTEGER NOT NULL,
    paths_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_version (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    note       TEXT,
    data_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exempt (
    path TEXT PRIMARY KEY             -- restored by the user: skip rules and zones once
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = {
    "operation_log": {"why": "TEXT"},
    "inbox_file": {"analysis_json": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
