"""Periodic synchronisation scheduler.

APScheduler embedded in the API process, running the same ``SyncService`` the API
and CLI use. There is exactly one implementation of "do a sync"; the scheduler
only decides *when*.

Why APScheduler rather than the BullMQ-equivalent (Celery + Redis): a single
periodic job does not justify a broker and a second daemon to operate. The
advisory lock — not the queue — is what guarantees runs do not overlap, and it
does so across *every* entry point, including a CLI run fired by hand or a systemd
timer on another host. Should NAS later need retries, fan-out or a work queue,
this module is the only thing that changes.

The CLI entrypoint (``nas sync run``) exists precisely so the scheduler can be
disabled (``NAS_SYNC_ENABLED=false``) and a systemd timer used instead, matching
how the CRM already schedules its jobs.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from nas.core.config import Settings
from nas.core.logging import get_logger
from nas.domain.enums import SyncTrigger
from nas.services.sync import SyncAlreadyRunningError, SyncService

logger = get_logger(__name__)

SYNC_JOB_ID = "periodic_vlan_sync"


class SyncScheduler:
    """Owns the APScheduler instance for the process lifetime."""

    def __init__(self, *, sync_service: SyncService, settings: Settings) -> None:
        self._sync_service = sync_service
        self._settings = settings
        self._scheduler = AsyncIOScheduler(timezone="UTC")

    async def _tick(self) -> None:
        """Run one scheduled sync.

        Must never raise: an exception escaping a job would be logged by
        APScheduler but could also stop the job being rescheduled on some
        configurations. A failed sync is normal operational noise, not a reason to
        stop syncing.
        """
        try:
            await self._sync_service.run(trigger=SyncTrigger.SCHEDULED)
        except SyncAlreadyRunningError:
            # Expected when a manual sync or a previous tick is still going.
            logger.info("scheduled_sync_skipped_already_running")
        except Exception:
            logger.exception("scheduled_sync_failed")

    def start(self) -> None:
        if not self._settings.sync_enabled:
            logger.info(
                "scheduler_disabled",
                detail="NAS_SYNC_ENABLED=false; drive syncs via 'nas sync run' instead.",
            )
            return

        self._scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(seconds=self._settings.sync_interval_seconds),
            id=SYNC_JOB_ID,
            name="Periodic VLAN synchronisation",
            # If the process was busy or paused, run once on resume rather than
            # firing every missed interval in a burst.
            coalesce=True,
            max_instances=1,
            # Don't sync the instant the process boots: let it become ready first,
            # and avoid a thundering herd if several instances restart together.
            next_run_time=None,
            misfire_grace_time=self._settings.sync_interval_seconds,
        )
        self._scheduler.start()
        logger.info(
            "scheduler_started",
            interval_seconds=self._settings.sync_interval_seconds,
            first_run_in_seconds=self._settings.sync_interval_seconds,
        )

    async def stop(self) -> None:
        if self._scheduler.running:
            # wait=False: shutdown should not block process exit on an in-flight
            # device poll, which can take as long as the command timeout. The
            # advisory lock is released by PostgreSQL when the connection drops.
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler_stopped")
