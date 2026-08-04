"""VLAN persistence against real PostgreSQL.

Covers what fakes cannot: the migration produces a working schema, plan
application actually writes what the plan says, timestamp semantics hold, and
SQL-level filters behave.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nas.domain.entities import Switch
from nas.domain.enums import InterfaceMode, Vendor, VlanState
from nas.domain.pagination import PageRequest
from nas.drivers.base import DiscoveredInterface, DiscoveredVlan
from nas.repositories.protocols import NewSwitch, VlanFilters
from nas.repositories.switches import SqlAlchemySwitchRepository
from nas.repositories.vlans import SqlAlchemyVlanRepository
from nas.sync.reconciler import build_plan

pytestmark = pytest.mark.integration

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


async def make_switch_row(session: AsyncSession, name: str, *, site: str = "ADC NBO") -> Switch:
    return await SqlAlchemySwitchRepository(session).create(
        NewSwitch(
            name=name,
            hostname=f"10.20.0.{abs(hash(name)) % 200 + 1}",
            vendor=Vendor.MOCK,
            credential_ref="mock-local",
            site=site,
        )
    )


def found(tag: int, **kwargs: object) -> DiscoveredVlan:
    return DiscoveredVlan(vlan_id=tag, **kwargs)  # type: ignore[arg-type]


async def sync_once(
    session: AsyncSession,
    switch: Switch,
    discovered: list[DiscoveredVlan],
    *,
    at: datetime,
    allow_empty: bool = False,
) -> None:
    repository = SqlAlchemyVlanRepository(session)
    existing = await repository.list_for_switch(switch.id)
    plan = build_plan(
        switch_id=switch.id,
        existing=existing,
        discovered=discovered,
        allow_empty_discovery=allow_empty,
    )
    await repository.apply_plan(plan, observed_at=at)
    await session.flush()


class TestApplyPlan:
    async def test_creates_vlans_with_interfaces(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(
            session,
            switch,
            [
                found(
                    110,
                    name="sip-safaricom",
                    description="Provider trunk",
                    l3_interface="irb.110",
                    interfaces=(
                        DiscoveredInterface(name="ge-0/0/1", mode=InterfaceMode.TRUNK),
                        DiscoveredInterface(name="ge-0/0/2", mode=InterfaceMode.ACCESS),
                    ),
                )
            ],
            at=T0,
        )

        vlans = await SqlAlchemyVlanRepository(session).list_for_switch(switch.id)
        assert len(vlans) == 1
        vlan = vlans[0]
        assert vlan.vlan_id == 110
        assert vlan.name == "sip-safaricom"
        assert vlan.l3_interface == "irb.110"
        assert vlan.state is VlanState.ACTIVE
        assert {i.name: i.mode for i in vlan.interfaces} == {
            "ge-0/0/1": InterfaceMode.TRUNK,
            "ge-0/0/2": InterfaceMode.ACCESS,
        }

    async def test_second_identical_sync_changes_nothing(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(110, name="v")], at=T0)
        before = (await SqlAlchemyVlanRepository(session).list_for_switch(switch.id))[0]

        await sync_once(session, switch, [found(110, name="v")], at=T1)
        after = (await SqlAlchemyVlanRepository(session).list_for_switch(switch.id))[0]

        assert after.id == before.id
        assert after.first_seen_at == before.first_seen_at
        # Seen again, so the timestamp advances even though nothing changed.
        assert after.last_seen_at == T1

    async def test_update_replaces_interfaces(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(
            session,
            switch,
            [found(110, interfaces=(DiscoveredInterface(name="ge-0/0/1"),))],
            at=T0,
        )
        await sync_once(
            session,
            switch,
            [
                found(
                    110,
                    interfaces=(
                        DiscoveredInterface(name="ge-0/0/9"),
                        DiscoveredInterface(name="ge-0/0/10"),
                    ),
                )
            ],
            at=T1,
        )
        vlan = (await SqlAlchemyVlanRepository(session).list_for_switch(switch.id))[0]
        assert sorted(i.name for i in vlan.interfaces) == ["ge-0/0/10", "ge-0/0/9"]

    async def test_absent_vlan_is_soft_deleted(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(110), found(120)], at=T0)
        await sync_once(session, switch, [found(110)], at=T1)

        records = await SqlAlchemyVlanRepository(session).list_for_switch(switch.id)
        vlans = {v.vlan_id: v for v in records}
        assert vlans[110].state is VlanState.ACTIVE
        assert vlans[120].state is VlanState.MISSING
        # The row survives — discovery data is an audit trail.
        assert vlans[120].first_seen_at == T0

    async def test_timestamp_semantics_differ_for_missing_vlans(
        self, session: AsyncSession
    ) -> None:
        """last_seen_at must NOT advance for a VLAN that was not seen, while
        last_synced_at must — the switch was polled successfully."""
        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(110), found(120)], at=T0)
        await sync_once(session, switch, [found(110)], at=T1)

        records = await SqlAlchemyVlanRepository(session).list_for_switch(switch.id)
        vlans = {v.vlan_id: v for v in records}
        assert vlans[120].last_seen_at == T0
        assert vlans[120].last_synced_at == T1

    async def test_reappearing_vlan_is_reactivated_not_duplicated(
        self, session: AsyncSession
    ) -> None:
        """The (switch_id, vlan_id) unique constraint makes this the difference
        between working and an IntegrityError."""
        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(110), found(120)], at=T0)
        await sync_once(session, switch, [found(110)], at=T1)
        await sync_once(session, switch, [found(110), found(120)], at=T2)

        vlans = await SqlAlchemyVlanRepository(session).list_for_switch(switch.id)
        assert len(vlans) == 2
        revived = next(v for v in vlans if v.vlan_id == 120)
        assert revived.state is VlanState.ACTIVE
        assert revived.first_seen_at == T0  # history preserved
        assert revived.last_seen_at == T2

    async def test_same_tag_on_two_switches_coexists(self, session: AsyncSession) -> None:
        switch_a = await make_switch_row(session, "sw-a")
        switch_b = await make_switch_row(session, "sw-b", site="iColo NBO1")
        await sync_once(session, switch_a, [found(1234)], at=T0)
        await sync_once(session, switch_b, [found(1234)], at=T0)

        records = await SqlAlchemyVlanRepository(session).find_by_tag(1234)
        assert len(records) == 2
        assert {r.switch_name for r in records} == {"sw-a", "sw-b"}
        assert {r.switch_site for r in records} == {"ADC NBO", "iColo NBO1"}

    async def test_deleting_a_switch_cascades_to_its_vlans(self, session: AsyncSession) -> None:
        from sqlalchemy import delete

        from nas.db.models import SwitchRow

        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(110)], at=T0)

        await session.execute(delete(SwitchRow).where(SwitchRow.id == switch.id))
        await session.flush()

        assert await SqlAlchemyVlanRepository(session).find_by_tag(110) == ()


class TestFindByTag:
    async def test_returns_active_and_missing(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(110), found(120)], at=T0)
        await sync_once(session, switch, [found(110)], at=T1)

        assert len(await SqlAlchemyVlanRepository(session).find_by_tag(120)) == 1

    async def test_unknown_tag_returns_empty(self, session: AsyncSession) -> None:
        assert await SqlAlchemyVlanRepository(session).find_by_tag(4000) == ()


class TestSearch:
    async def test_filters_by_state(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(110), found(120)], at=T0)
        await sync_once(session, switch, [found(110)], at=T1)

        page = await SqlAlchemyVlanRepository(session).search(
            filters=VlanFilters(state=VlanState.MISSING), page_request=PageRequest()
        )
        assert [v.vlan_id for v in page.items] == [120]

    async def test_filters_by_site_case_insensitively(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a", site="iColo NBO1")
        await sync_once(session, switch, [found(110)], at=T0)

        page = await SqlAlchemyVlanRepository(session).search(
            filters=VlanFilters(site="icolo nbo1"), page_request=PageRequest()
        )
        assert page.total == 1

    async def test_search_matches_interface_name(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(
            session,
            switch,
            [
                found(110, interfaces=(DiscoveredInterface(name="ge-0/0/47"),)),
                found(120, interfaces=(DiscoveredInterface(name="xe-1/1/1"),)),
            ],
            at=T0,
        )
        page = await SqlAlchemyVlanRepository(session).search(
            filters=VlanFilters(search="ge-0/0/47"), page_request=PageRequest()
        )
        assert [v.vlan_id for v in page.items] == [110]

    async def test_vlan_with_many_matching_ports_is_returned_once(
        self, session: AsyncSession
    ) -> None:
        """EXISTS rather than a join — otherwise the row would be duplicated."""
        switch = await make_switch_row(session, "sw-a")
        await sync_once(
            session,
            switch,
            [
                found(
                    110,
                    interfaces=(
                        DiscoveredInterface(name="ge-0/0/1"),
                        DiscoveredInterface(name="ge-0/0/2"),
                        DiscoveredInterface(name="ge-0/0/3"),
                    ),
                )
            ],
            at=T0,
        )
        page = await SqlAlchemyVlanRepository(session).search(
            filters=VlanFilters(search="ge-0/0/"), page_request=PageRequest()
        )
        assert page.total == 1
        assert len(page.items) == 1

    async def test_wildcards_in_search_are_escaped(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(
            session,
            switch,
            [found(110, description="100% utilised"), found(120, description="plain")],
            at=T0,
        )
        page = await SqlAlchemyVlanRepository(session).search(
            filters=VlanFilters(search="100%"), page_request=PageRequest()
        )
        assert [v.vlan_id for v in page.items] == [110]

    async def test_ordering_and_pagination(self, session: AsyncSession) -> None:
        switch = await make_switch_row(session, "sw-a")
        await sync_once(session, switch, [found(t) for t in (300, 100, 200, 400)], at=T0)

        page = await SqlAlchemyVlanRepository(session).search(
            filters=VlanFilters(), page_request=PageRequest(page=1, page_size=3)
        )
        assert [v.vlan_id for v in page.items] == [100, 200, 300]
        assert page.total == 4
        assert page.has_next is True


class TestConstraints:
    async def test_duplicate_tag_on_one_switch_is_rejected(self, session: AsyncSession) -> None:
        from sqlalchemy.exc import IntegrityError

        from nas.db.models import VlanRow

        switch = await make_switch_row(session, "sw-a")
        for _ in range(2):
            session.add(
                VlanRow(
                    switch_id=switch.id,
                    vlan_id=110,
                    state=VlanState.ACTIVE.value,
                    first_seen_at=T0,
                    last_seen_at=T0,
                    last_synced_at=T0,
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()

    @pytest.mark.parametrize("tag", [0, 4095, -5])
    async def test_out_of_range_tag_is_rejected_by_the_database(
        self, session: AsyncSession, tag: int
    ) -> None:
        """Defence in depth: the DTO validates too, but the constraint holds for
        any writer, including psql."""
        from sqlalchemy.exc import DBAPIError, IntegrityError

        from nas.db.models import VlanRow

        switch = await make_switch_row(session, "sw-a")
        session.add(
            VlanRow(
                switch_id=switch.id,
                vlan_id=tag,
                state=VlanState.ACTIVE.value,
                first_seen_at=T0,
                last_seen_at=T0,
                last_synced_at=T0,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.flush()
