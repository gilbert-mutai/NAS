"""SQLAlchemy implementation of AuditRepository.

Append and read only. There is no update and no delete, and that is a design
constraint rather than an omission — see the Protocol.
"""

from __future__ import annotations

from sqlalchemy import Select, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from nas.db.models import AuditLogRow
from nas.domain.entities import AuditEntry
from nas.domain.enums import AuditAction, AuditOutcome
from nas.domain.pagination import Page, PageRequest
from nas.repositories.protocols import AuditFilters


def to_entity(row: AuditLogRow) -> AuditEntry:
    return AuditEntry(
        id=row.id,
        action=AuditAction(row.action),
        outcome=AuditOutcome(row.outcome),
        occurred_at=row.occurred_at,
        api_key_id=row.api_key_id,
        api_key_name=row.api_key_name,
        actor=row.actor,
        source_ip=row.source_ip,
        correlation_id=row.correlation_id,
        target_type=row.target_type,
        target_id=row.target_id,
        detail=row.detail,
    )


class SqlAlchemyAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, entry: AuditEntry) -> AuditEntry:
        result = await self._session.execute(
            insert(AuditLogRow)
            .values(
                action=entry.action.value,
                outcome=entry.outcome.value,
                occurred_at=entry.occurred_at,
                api_key_id=entry.api_key_id,
                api_key_name=entry.api_key_name,
                actor=entry.actor,
                source_ip=entry.source_ip,
                correlation_id=entry.correlation_id,
                target_type=entry.target_type,
                target_id=entry.target_id,
                detail=entry.detail,
            )
            .returning(AuditLogRow)
        )
        return to_entity(result.scalar_one())

    def _apply_filters(
        self, stmt: Select[tuple[AuditLogRow]], filters: AuditFilters
    ) -> Select[tuple[AuditLogRow]]:
        if filters.action is not None:
            stmt = stmt.where(AuditLogRow.action == filters.action.value)
        if filters.outcome is not None:
            stmt = stmt.where(AuditLogRow.outcome == filters.outcome.value)
        if filters.actor:
            # Case-insensitive exact match. Actors are email addresses, whose local
            # part is technically case-sensitive but never treated as such in
            # practice — "Gilbert@..." must find the same rows as "gilbert@...".
            stmt = stmt.where(func.lower(AuditLogRow.actor) == filters.actor.lower())
        if filters.since is not None:
            stmt = stmt.where(AuditLogRow.occurred_at >= filters.since)
        return stmt

    async def list(self, *, filters: AuditFilters, page_request: PageRequest) -> Page[AuditEntry]:
        count_stmt = self._apply_filters(select(AuditLogRow), filters).with_only_columns(
            func.count(AuditLogRow.id), maintain_column_froms=True
        )
        total = (await self._session.execute(count_stmt)).scalar_one()

        stmt = self._apply_filters(select(AuditLogRow), filters)
        # id breaks ties: two entries can share a timestamp, and an unstable order
        # would let a row appear on two pages or on neither.
        stmt = (
            stmt.order_by(AuditLogRow.occurred_at.desc(), AuditLogRow.id.desc())
            .offset(page_request.offset)
            .limit(page_request.limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return Page(
            items=tuple(to_entity(row) for row in rows),
            total=total,
            page=page_request.page,
            page_size=page_request.page_size,
        )
