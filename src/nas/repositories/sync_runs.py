"""SQLAlchemy implementation of SyncRunRepository."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nas.core.errors import NotFoundError
from nas.db.models import SyncRunRow, SyncRunSwitchRow
from nas.domain.entities import SyncRun, SyncRunSwitch
from nas.domain.enums import SwitchSyncOutcome, SyncStatus, SyncTrigger
from nas.domain.pagination import Page, PageRequest
from nas.repositories.protocols import SwitchSyncResult, SyncRunTotals


def _switch_to_entity(row: SyncRunSwitchRow) -> SyncRunSwitch:
    return SyncRunSwitch(
        id=row.id,
        sync_run_id=row.sync_run_id,
        switch_id=row.switch_id,
        switch_name=row.switch_name,
        outcome=SwitchSyncOutcome(row.outcome),
        vlans_discovered=row.vlans_discovered,
        vlans_created=row.vlans_created,
        vlans_updated=row.vlans_updated,
        vlans_unchanged=row.vlans_unchanged,
        vlans_marked_missing=row.vlans_marked_missing,
        duration_ms=row.duration_ms,
        error_message=row.error_message,
    )


def to_entity(row: SyncRunRow, *, include_switches: bool = True) -> SyncRun:
    return SyncRun(
        id=row.id,
        trigger=SyncTrigger(row.trigger),
        status=SyncStatus(row.status),
        started_at=row.started_at,
        correlation_id=row.correlation_id,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        switches_total=row.switches_total,
        switches_succeeded=row.switches_succeeded,
        switches_failed=row.switches_failed,
        switches_skipped=row.switches_skipped,
        vlans_discovered=row.vlans_discovered,
        vlans_created=row.vlans_created,
        vlans_updated=row.vlans_updated,
        vlans_unchanged=row.vlans_unchanged,
        vlans_marked_missing=row.vlans_marked_missing,
        error_message=row.error_message,
        switch_results=(
            tuple(_switch_to_entity(item) for item in row.switch_results)
            if include_switches
            else ()
        ),
    )


class SqlAlchemySyncRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self, *, trigger: SyncTrigger, correlation_id: str, started_at: datetime
    ) -> SyncRun:
        result = await self._session.execute(
            insert(SyncRunRow)
            .values(
                trigger=trigger.value,
                status=SyncStatus.RUNNING.value,
                started_at=started_at,
                correlation_id=correlation_id,
            )
            .returning(SyncRunRow.id)
        )
        run_id = result.scalar_one()
        # Committed immediately so /sync/status reflects an in-flight run rather
        # than only appearing once the run finishes.
        await self._session.commit()
        return await self._require(run_id)

    async def record_switch(self, sync_run_id: int, result: SwitchSyncResult) -> None:
        await self._session.execute(
            insert(SyncRunSwitchRow).values(
                sync_run_id=sync_run_id,
                switch_id=result.switch_id,
                switch_name=result.switch_name,
                outcome=result.outcome.value,
                vlans_discovered=result.vlans_discovered,
                vlans_created=result.vlans_created,
                vlans_updated=result.vlans_updated,
                vlans_unchanged=result.vlans_unchanged,
                vlans_marked_missing=result.vlans_marked_missing,
                duration_ms=result.duration_ms,
                error_message=result.error_message,
            )
        )

    async def finish(
        self,
        sync_run_id: int,
        *,
        status: SyncStatus,
        finished_at: datetime,
        duration_ms: int,
        totals: SyncRunTotals,
        error_message: str | None = None,
    ) -> SyncRun:
        await self._session.execute(
            update(SyncRunRow)
            .where(SyncRunRow.id == sync_run_id)
            .values(
                status=status.value,
                finished_at=finished_at,
                duration_ms=duration_ms,
                switches_total=totals.switches_total,
                switches_succeeded=totals.switches_succeeded,
                switches_failed=totals.switches_failed,
                switches_skipped=totals.switches_skipped,
                vlans_discovered=totals.vlans_discovered,
                vlans_created=totals.vlans_created,
                vlans_updated=totals.vlans_updated,
                vlans_unchanged=totals.vlans_unchanged,
                vlans_marked_missing=totals.vlans_marked_missing,
                error_message=error_message,
            )
            .execution_options(synchronize_session=False)
        )
        return await self._require(sync_run_id)

    async def get_by_id(self, sync_run_id: int) -> SyncRun | None:
        stmt = (
            select(SyncRunRow)
            .options(selectinload(SyncRunRow.switch_results))
            .where(SyncRunRow.id == sync_run_id)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_entity(row) if row else None

    async def latest(self) -> SyncRun | None:
        stmt = (
            select(SyncRunRow)
            .options(selectinload(SyncRunRow.switch_results))
            .order_by(SyncRunRow.started_at.desc(), SyncRunRow.id.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_entity(row) if row else None

    async def list(self, *, page_request: PageRequest) -> Page[SyncRun]:
        total = (
            await self._session.execute(select(func.count()).select_from(SyncRunRow))
        ).scalar_one()
        stmt = (
            select(SyncRunRow)
            # Per-switch detail is omitted from the list view: it is only needed
            # on a single run, and loading it for every row is wasted work.
            .order_by(SyncRunRow.started_at.desc(), SyncRunRow.id.desc())
            .offset(page_request.offset)
            .limit(page_request.limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return Page(
            items=tuple(to_entity(row, include_switches=False) for row in rows),
            total=total,
            page=page_request.page,
            page_size=page_request.page_size,
        )

    async def fail_stale_runs(self, *, older_than: datetime) -> int:
        result = await self._session.execute(
            update(SyncRunRow)
            .where(
                SyncRunRow.status == SyncStatus.RUNNING.value,
                SyncRunRow.started_at < older_than,
            )
            .values(
                status=SyncStatus.FAILED.value,
                error_message=(
                    "Run abandoned — the service stopped before it completed. "
                    "Marked failed automatically."
                ),
            )
            .execution_options(synchronize_session=False)
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def _require(self, sync_run_id: int) -> SyncRun:
        run = await self.get_by_id(sync_run_id)
        if run is None:
            raise NotFoundError(f"Sync run {sync_run_id} disappeared while in progress.")
        return run
