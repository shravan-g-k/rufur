"""Background maintenance inside the daemon: the STM sweeper and nightly consolidation.

One thread wakes every few seconds for idle extraction and once a minute for maintenance. Work is handed to the service's single worker so it
never runs concurrently with session extraction.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING

from .textutil import now
from .write.consolidate import consolidate, yesterday

if TYPE_CHECKING:
    from .service import MemoryService

log = logging.getLogger("rufur.scheduler")


class Scheduler:
    def __init__(self, svc: "MemoryService", tick_s: float = 15.0, maintenance_s: float = 60.0):
        self.svc = svc
        self.tick_s = tick_s  # idle-session extraction runs every tick
        self.maintenance_s = maintenance_s  # sweep and nightly checks; they must not re-queue a job still pending
        self._last_maintenance = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="rufur-scheduler", daemon=True)

    def start(self) -> None:
        self._catch_up()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _users(self) -> list[str]:
        return [r["user_id"] for r in self.svc.db.all(
            "SELECT DISTINCT user_id FROM agents WHERE user_id NOT LIKE 'scratch:%'")] or [self.svc.cfg.default_user]  # fmt: skip

    def _last_run(self, user: str, trigger: str | None = None) -> float:
        sql = "SELECT max(started_at) FROM consolidation_runs WHERE user_id = ?"
        params: tuple = (user,)
        if trigger:
            sql += " AND trigger = ?"
            params += (trigger,)
        return self.svc.db.scalar(sql, params) or 0.0

    def _submit(self, fn, *args, **kw) -> None:
        def run():
            try:
                fn(*args, **kw)
            except Exception:  # keep the daemon alive; the next tick retries
                log.exception("scheduled job failed")

        self.svc.worker.submit(run)

    def _catch_up(self) -> None:
        """The laptop may have been asleep at 03:00; consolidate now if the last run is old."""
        for user in self._users():
            if now() - self._last_run(user) > self.svc.cfg.catch_up_after_h * 3600:
                self._submit(consolidate, self.svc, user, "catch_up", journal_day=yesterday())

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.svc.extract_idle_sessions()
                if now() - self._last_maintenance >= self.maintenance_s:
                    self._last_maintenance = now()
                    self.tick()
            except Exception:  # keep the scheduler thread alive; the next tick retries
                log.exception("scheduler tick failed")

    def tick(self) -> None:
        c = self.svc.cfg
        last_sweep = float(self.svc.db.meta("last_sweep") or 0)
        if now() - last_sweep >= c.sweep_every_min * 60:
            self._submit(self.svc.sweep)
        local = datetime.fromtimestamp(now())
        if local.hour >= c.nightly_hour:
            today_start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            for user in self._users():
                if self._last_run(user, "nightly") < today_start:
                    self._submit(consolidate, self.svc, user, "nightly", journal_day=yesterday())
