from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class StateStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                execute INTEGER NOT NULL,
                status TEXT NOT NULL,
                summary_json TEXT
            );

            CREATE TABLE IF NOT EXISTS placements (
                sha TEXT PRIMARY KEY,
                destination_rel TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                run_id INTEGER NOT NULL,
                month_key TEXT,
                source_name TEXT
            );

            CREATE TABLE IF NOT EXISTS files (
                source_path TEXT PRIMARY KEY,
                sha TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                month_key TEXT NOT NULL,
                first_seen_run_id INTEGER NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                source_path TEXT NOT NULL,
                destination_path TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                sha TEXT
            );
            """
        )
        self._ensure_column("placements", "month_key", "TEXT")
        self._ensure_column("placements", "source_name", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_def: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {str(r[1]) for r in rows}
        if column not in names:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")

    def start_run(self, *, mode: str, execute: bool) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, mode, execute, status) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(timespec="seconds"), mode, int(execute), "running"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, status: str, summary: dict[str, int]) -> None:
        self.conn.execute(
            "UPDATE runs SET status = ?, summary_json = ? WHERE id = ?",
            (status, json.dumps(summary), run_id),
        )
        self.conn.commit()

    def get_placement_by_sha(self, sha: str) -> str | None:
        row = self.conn.execute(
            "SELECT destination_rel FROM placements WHERE sha = ?",
            (sha,),
        ).fetchone()
        if row is None:
            return None
        return str(row["destination_rel"])

    def upsert_placement(
        self,
        *,
        sha: str,
        destination_rel: str,
        run_id: int,
        month_key: str | None,
        source_name: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO placements(sha, destination_rel, applied_at, run_id, month_key, source_name)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha) DO UPDATE SET
                destination_rel = excluded.destination_rel,
                applied_at = excluded.applied_at,
                run_id = excluded.run_id,
                month_key = excluded.month_key,
                source_name = excluded.source_name
            """,
            (
                sha,
                destination_rel,
                datetime.utcnow().isoformat(timespec="seconds"),
                run_id,
                month_key,
                source_name,
            ),
        )
        self.conn.commit()

    def record_file_snapshot(
        self,
        *,
        source_path: str,
        sha: str,
        size: int,
        mtime_ns: int,
        month_key: str,
        run_id: int,
    ) -> bool:
        row = self.conn.execute(
            "SELECT sha, size, mtime_ns, month_key FROM files WHERE source_path = ?",
            (source_path,),
        ).fetchone()

        changed = row is None or str(row["sha"]) != sha or int(row["size"]) != size or int(row["mtime_ns"]) != mtime_ns or str(row["month_key"]) != month_key

        if row is None:
            self.conn.execute(
                """
                INSERT INTO files(source_path, sha, size, mtime_ns, month_key, first_seen_run_id, last_seen_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source_path, sha, size, mtime_ns, month_key, run_id, run_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE files
                SET sha = ?, size = ?, mtime_ns = ?, month_key = ?, last_seen_run_id = ?
                WHERE source_path = ?
                """,
                (sha, size, mtime_ns, month_key, run_id, source_path),
            )
        self.conn.commit()
        return changed

    def get_placements_for_months(self, month_keys: set[str]) -> list[sqlite3.Row]:
        if not month_keys:
            return []
        placeholders = ",".join("?" for _ in month_keys)
        query = (
            "SELECT sha, destination_rel, month_key, source_name FROM placements "
            f"WHERE month_key IN ({placeholders})"
        )
        return self.conn.execute(query, tuple(sorted(month_keys))).fetchall()

    def record_operation(
        self,
        *,
        run_id: int,
        source_path: str,
        destination_path: str | None,
        action: str,
        status: str,
        error: str | None,
        sha: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO operations(run_id, source_path, destination_path, action, status, error, sha)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, source_path, destination_path, action, status, error, sha),
        )
        self.conn.commit()