"""Background loop: runs a pass every N minutes inside the allowed hours."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db, jobs

log = logging.getLogger("synopixtri")


class Scheduler:
    def __init__(self, boot: config.Bootstrap, tick_s: float = 15.0) -> None:
        self.boot, self.tick_s = boot, tick_s
        self.next_run_at: float | None = None
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _settings(self) -> dict:
        self.boot.data_dir.mkdir(parents=True, exist_ok=True)
        conn = db.connect(self.boot.db_path)
        try:
            db.init(conn)
            return config.load(conn)
        finally:
            conn.close()

    def start(self) -> None:
        values = self._settings()
        delay = values["startup_delay_s"] if values["run_on_startup"] else values["scan_interval_min"] * 60
        self.next_run_at = time.time() + float(delay)  # type: ignore[arg-type]
        self._thread = threading.Thread(target=self._loop, name="synopixtri-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_now(self, *, dry_run: bool = False, skip_brake: bool = False) -> None:
        """Start a pass in the background. Raises jobs.Busy if one is already running."""
        if not jobs._LOCK.acquire(blocking=False):  # noqa: SLF001
            raise jobs.Busy()
        jobs._LOCK.release()  # noqa: SLF001 - only a probe, run_pass takes it again
        threading.Thread(
            target=self._safe_pass, kwargs={"dry_run": dry_run, "skip_brake": skip_brake}, daemon=True
        ).start()

    def _safe_pass(self, **kwargs) -> None:
        try:
            jobs.run_pass(self.boot, **kwargs)
            self.last_error = None
        except jobs.Busy:
            pass
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            if self.next_run_at is None or time.time() < self.next_run_at:
                continue
            try:
                values = self._settings()
                local_now = datetime.now(ZoneInfo(str(values["timezone"])))
                if not config.in_active_hours(values["active_hours"], local_now):  # type: ignore[arg-type]
                    continue
                self._safe_pass()
                self.next_run_at = time.time() + float(values["scan_interval_min"]) * 60  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - the loop must survive any error
                log.exception("scheduler tick failed")
                self.next_run_at = time.time() + 300
