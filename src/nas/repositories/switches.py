"""SQLAlchemy implementation of SwitchRepository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nas.core.errors import ConflictError
from nas.db.models import SwitchRow
from nas.domain.entities import Switch
from nas.domain.enums import Vendor
from nas.domain.pagination import Page, PageRequest
from nas.repositories.protocols import NewSwitch, SwitchFilters


def to_entity(row: SwitchRow) -> Switch:
    return Switch(
        id=row.id,
        name=row.name,
        hostname=row.hostname,
        port=row.port,
        vendor=Vendor(row.vendor),
        credential_ref=row.credential_ref,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        site=row.site,
        environment=row.environment,
        model=row.model,
        os_version=row.os_version,
        description=row.description,
        is_reachable=row.is_reachable,
        last_health_check=row.last_health_check,
        health_error=row.health_error,
    )


class SqlAlchemySwitchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, switch_id: int) -> Switch | None:
        row = await self._session.get(SwitchRow, switch_id)
        return to_entity(row) if row else None

    async def get_by_name(self, name: str) -> Switch | None:
        stmt = select(SwitchRow).where(func.lower(SwitchRow.name) == name.strip().lower())
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_entity(row) if row else None

    async def list(self, *, filters: SwitchFilters, page_request: PageRequest) -> Page[Switch]:
        stmt = self._apply_filters(select(SwitchRow), filters)

        # Count over the filtered set before applying limit/offset, so `total`
        # reflects the whole result set rather than the current page.
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self._session.execute(count_stmt)).scalar_one()

        stmt = (
            stmt.order_by(SwitchRow.name.asc())
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

    async def create(self, data: NewSwitch) -> Switch:
        row = SwitchRow(
            name=data.name.strip(),
            hostname=data.hostname.strip(),
            port=data.port,
            vendor=data.vendor.value,
            credential_ref=data.credential_ref.strip(),
            site=data.site,
            environment=data.environment,
            description=data.description,
            is_active=data.is_active,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                f"A switch named {data.name!r} already exists.",
                details={"field": "name"},
            ) from exc
        await self._session.refresh(row)
        return to_entity(row)

    async def record_observation(
        self,
        switch_id: int,
        *,
        is_reachable: bool,
        checked_at: datetime,
        health_error: str | None = None,
        model: str | None = None,
        os_version: str | None = None,
    ) -> None:
        """Record what a sync attempt learned about a device.

        Facts are only overwritten when the device actually reported them: a
        failed poll must not blank out the model and OS version discovered by the
        last successful one.
        """
        values: dict[str, object] = {
            "is_reachable": is_reachable,
            "last_health_check": checked_at,
            "health_error": health_error,
        }
        if model is not None:
            values["model"] = model
        if os_version is not None:
            values["os_version"] = os_version

        await self._session.execute(
            update(SwitchRow)
            .where(SwitchRow.id == switch_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[SwitchRow]], filters: SwitchFilters
    ) -> Select[tuple[SwitchRow]]:
        if filters.vendor is not None:
            stmt = stmt.where(SwitchRow.vendor == filters.vendor.value)
        if filters.site:
            stmt = stmt.where(func.lower(SwitchRow.site) == filters.site.strip().lower())
        if filters.environment:
            stmt = stmt.where(
                func.lower(SwitchRow.environment) == filters.environment.strip().lower()
            )
        if filters.is_active is not None:
            stmt = stmt.where(SwitchRow.is_active.is_(filters.is_active))
        if filters.search:
            # Parameterised LIKE with escaped wildcards — a search term containing
            # % or _ matches literally instead of broadening the query.
            term = f"%{_escape_like(filters.search.strip())}%"
            stmt = stmt.where(
                or_(
                    SwitchRow.name.ilike(term, escape="\\"),
                    SwitchRow.hostname.ilike(term, escape="\\"),
                    SwitchRow.description.ilike(term, escape="\\"),
                )
            )
        return stmt


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
