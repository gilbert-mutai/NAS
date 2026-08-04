"""SQLAlchemy implementation of VlanRepository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nas.db.models import SwitchRow, VlanInterfaceRow, VlanRow
from nas.domain.entities import Vlan, VlanInterface
from nas.domain.enums import InterfaceMode, VlanState
from nas.domain.pagination import Page, PageRequest
from nas.drivers.base import DiscoveredInterface, DiscoveredVlan
from nas.repositories.protocols import VlanFilters
from nas.sync.reconciler import ReconciliationPlan, VlanUpdate


def _interface_to_entity(row: VlanInterfaceRow) -> VlanInterface:
    return VlanInterface(id=row.id, name=row.name, mode=InterfaceMode(row.mode))


def to_entity(row: VlanRow, *, switch: SwitchRow | None = None) -> Vlan:
    return Vlan(
        id=row.id,
        switch_id=row.switch_id,
        vlan_id=row.vlan_id,
        state=VlanState(row.state),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        last_synced_at=row.last_synced_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        name=row.name,
        description=row.description,
        l3_interface=row.l3_interface,
        vxlan_vni=row.vxlan_vni,
        interfaces=tuple(_interface_to_entity(item) for item in row.interfaces),
        switch_name=switch.name if switch else None,
        switch_site=switch.site if switch else None,
    )


class SqlAlchemyVlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Reads ─────────────────────────────────────────────────────────────────
    async def get_by_id(self, vlan_record_id: int) -> Vlan | None:
        stmt = (
            select(VlanRow, SwitchRow)
            .join(SwitchRow, SwitchRow.id == VlanRow.switch_id)
            .options(selectinload(VlanRow.interfaces))
            .where(VlanRow.id == vlan_record_id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        vlan_row, switch_row = row
        return to_entity(vlan_row, switch=switch_row)

    async def list_for_switch(self, switch_id: int) -> tuple[Vlan, ...]:
        stmt = (
            select(VlanRow)
            .options(selectinload(VlanRow.interfaces))
            .where(VlanRow.switch_id == switch_id)
            .order_by(VlanRow.vlan_id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return tuple(to_entity(row) for row in rows)

    async def find_by_tag(self, vlan_id: int) -> tuple[Vlan, ...]:
        stmt = (
            select(VlanRow, SwitchRow)
            .join(SwitchRow, SwitchRow.id == VlanRow.switch_id)
            .options(selectinload(VlanRow.interfaces))
            .where(VlanRow.vlan_id == vlan_id)
            .order_by(SwitchRow.name.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        return tuple(to_entity(vlan_row, switch=switch_row) for vlan_row, switch_row in rows)

    async def search(self, *, filters: VlanFilters, page_request: PageRequest) -> Page[Vlan]:
        base: Select[Any] = select(VlanRow.id).join(SwitchRow, SwitchRow.id == VlanRow.switch_id)
        base = self._apply_filters(base, filters)

        total = (
            await self._session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()

        stmt: Select[Any] = (
            select(VlanRow, SwitchRow)
            .join(SwitchRow, SwitchRow.id == VlanRow.switch_id)
            .options(selectinload(VlanRow.interfaces))
        )
        stmt = self._apply_filters(stmt, filters)
        stmt = (
            stmt.order_by(VlanRow.vlan_id.asc(), SwitchRow.name.asc())
            .offset(page_request.offset)
            .limit(page_request.limit)
        )
        rows = (await self._session.execute(stmt)).all()

        return Page(
            items=tuple(to_entity(vlan_row, switch=switch_row) for vlan_row, switch_row in rows),
            total=total,
            page=page_request.page,
            page_size=page_request.page_size,
        )

    @staticmethod
    def _apply_filters(stmt: Select[Any], filters: VlanFilters) -> Select[Any]:
        if filters.vlan_id is not None:
            stmt = stmt.where(VlanRow.vlan_id == filters.vlan_id)
        if filters.switch_id is not None:
            stmt = stmt.where(VlanRow.switch_id == filters.switch_id)
        if filters.state is not None:
            stmt = stmt.where(VlanRow.state == filters.state.value)
        if filters.site:
            stmt = stmt.where(func.lower(SwitchRow.site) == filters.site.strip().lower())
        if filters.search:
            term = f"%{_escape_like(filters.search.strip())}%"
            # Interface membership is matched with EXISTS rather than a join, so a
            # VLAN with several matching ports is not returned multiple times.
            interface_match = exists(
                select(VlanInterfaceRow.id).where(
                    VlanInterfaceRow.vlan_record_id == VlanRow.id,
                    VlanInterfaceRow.name.ilike(term, escape="\\"),
                )
            )
            stmt = stmt.where(
                or_(
                    VlanRow.name.ilike(term, escape="\\"),
                    VlanRow.description.ilike(term, escape="\\"),
                    interface_match,
                )
            )
        return stmt

    # ── Writes ────────────────────────────────────────────────────────────────
    async def apply_plan(self, plan: ReconciliationPlan, *, observed_at: datetime) -> None:
        """Persist a plan.

        Runs inside the caller's transaction, so a failure part-way leaves the
        switch's VLAN data exactly as it was rather than half-updated.

        Note the timestamp discipline:
          * ``last_seen_at``   advances only for VLANs actually observed.
          * ``last_synced_at`` advances for every record touched, including ones
            marked missing — the switch *was* polled successfully.
        Conflating the two would make staleness reporting meaningless.
        """
        for discovered in plan.to_create:
            await self._insert(plan.switch_id, discovered, observed_at=observed_at)

        for item in plan.to_update:
            await self._update(item, observed_at=observed_at)

        if plan.unchanged:
            # Bulk timestamp touch: nothing about these VLANs changed, but they
            # were seen, and staleness depends on recording that.
            await self._session.execute(
                update(VlanRow)
                .where(VlanRow.id.in_([record.id for record in plan.unchanged]))
                .values(last_seen_at=observed_at, last_synced_at=observed_at)
                .execution_options(synchronize_session=False)
            )

        if plan.to_mark_missing:
            await self._session.execute(
                update(VlanRow)
                .where(VlanRow.id.in_([record.id for record in plan.to_mark_missing]))
                .values(state=VlanState.MISSING.value, last_synced_at=observed_at)
                .execution_options(synchronize_session=False)
            )

    async def _insert(
        self, switch_id: int, discovered: DiscoveredVlan, *, observed_at: datetime
    ) -> None:
        result = await self._session.execute(
            insert(VlanRow)
            .values(
                switch_id=switch_id,
                vlan_id=discovered.vlan_id,
                name=discovered.name,
                description=discovered.description,
                l3_interface=discovered.l3_interface,
                vxlan_vni=discovered.vxlan_vni,
                state=VlanState.ACTIVE.value,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                last_synced_at=observed_at,
                raw=discovered.raw or None,
            )
            .returning(VlanRow.id)
        )
        await self._replace_interfaces(result.scalar_one(), discovered.interfaces)

    async def _update(self, item: VlanUpdate, *, observed_at: datetime) -> None:
        discovered = item.discovered
        await self._session.execute(
            update(VlanRow)
            .where(VlanRow.id == item.existing.id)
            .values(
                name=discovered.name,
                description=discovered.description,
                l3_interface=discovered.l3_interface,
                vxlan_vni=discovered.vxlan_vni,
                # A reappearing VLAN returns to active.
                state=VlanState.ACTIVE.value,
                last_seen_at=observed_at,
                last_synced_at=observed_at,
                raw=discovered.raw or None,
            )
            .execution_options(synchronize_session=False)
        )
        if "interfaces" in item.changed_fields or item.reactivated:
            await self._replace_interfaces(item.existing.id, discovered.interfaces)

    async def _replace_interfaces(
        self, vlan_record_id: int, interfaces: Sequence[DiscoveredInterface]
    ) -> None:
        """Replace port membership wholesale.

        Simpler and cheaper than diffing individual rows, and correct: membership
        is small and always fully reported by the driver.
        """
        await self._session.execute(
            delete(VlanInterfaceRow).where(VlanInterfaceRow.vlan_record_id == vlan_record_id)
        )
        payload = [
            {
                "vlan_record_id": vlan_record_id,
                "name": interface.name,
                "mode": interface.mode.value,
            }
            for interface in interfaces
        ]
        if payload:
            await self._session.execute(insert(VlanInterfaceRow), payload)


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
