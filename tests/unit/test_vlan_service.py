"""VLAN query use-cases, especially the availability verdict."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nas.core.errors import NotFoundError, ValidationError
from nas.domain.entities import SyncRun, VlanInterface
from nas.domain.enums import (
    InterfaceMode,
    SyncStatus,
    SyncTrigger,
    VlanAvailability,
    VlanState,
)
from nas.domain.pagination import PageRequest
from nas.repositories.protocols import VlanFilters
from nas.services.vlans import VlanService
from tests.fakes import InMemorySyncRunRepository, InMemoryVlanRepository, make_vlan

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def build_service(
    vlans: list[object] | None = None,
    *,
    last_finished: datetime | None = NOW,
    staleness: timedelta = timedelta(hours=6),
) -> VlanService:
    runs = InMemorySyncRunRepository()
    if last_finished is not None:
        runs.seed(
            SyncRun(
                id=1,
                trigger=SyncTrigger.SCHEDULED,
                status=SyncStatus.SUCCESS,
                started_at=last_finished - timedelta(seconds=5),
                correlation_id="abc",
                finished_at=last_finished,
            )
        )
    return VlanService(
        repository=InMemoryVlanRepository(vlans or []),  # type: ignore[arg-type]
        sync_runs=runs,
        staleness_threshold=staleness,
    )


class TestAvailabilityVerdict:
    async def test_unused_tag_is_available(self) -> None:
        lookup = await build_service([]).lookup(1234, now=NOW)
        assert lookup.availability is VlanAvailability.AVAILABLE
        assert lookup.is_available is True
        assert lookup.switch_count == 0

    async def test_tag_on_one_switch_is_in_use(self) -> None:
        vlans = [make_vlan(record_id=1, vlan_id=1234, switch_id=1, switch_name="sw-a")]
        lookup = await build_service(vlans).lookup(1234, now=NOW)
        assert lookup.availability is VlanAvailability.IN_USE
        assert lookup.is_available is False
        assert lookup.switch_count == 1

    async def test_tag_on_several_switches_aggregates(self) -> None:
        """The question is 'who has 1234', across the whole estate."""
        vlans = [
            make_vlan(record_id=1, vlan_id=1234, switch_id=1, switch_name="sw-a"),
            make_vlan(record_id=2, vlan_id=1234, switch_id=2, switch_name="sw-b"),
            make_vlan(record_id=3, vlan_id=1234, switch_id=3, switch_name="sw-c"),
        ]
        lookup = await build_service(vlans).lookup(1234, now=NOW)
        assert lookup.switch_count == 3
        assert [u.switch_name for u in lookup.active_usages] == ["sw-a", "sw-b", "sw-c"]

    async def test_only_missing_records_means_available(self) -> None:
        """A tag whose records are all soft-deleted is free to reuse."""
        vlans = [make_vlan(record_id=1, vlan_id=1234, state=VlanState.MISSING)]
        lookup = await build_service(vlans).lookup(1234, now=NOW)
        assert lookup.availability is VlanAvailability.AVAILABLE
        assert len(lookup.historic_usages) == 1
        assert lookup.active_usages == ()

    async def test_active_and_missing_are_reported_separately(self) -> None:
        vlans = [
            make_vlan(record_id=1, vlan_id=1234, switch_id=1, switch_name="sw-a"),
            make_vlan(
                record_id=2,
                vlan_id=1234,
                switch_id=2,
                switch_name="sw-b",
                state=VlanState.MISSING,
            ),
        ]
        lookup = await build_service(vlans).lookup(1234, now=NOW)
        assert lookup.availability is VlanAvailability.IN_USE
        assert [u.switch_name for u in lookup.active_usages] == ["sw-a"]
        assert [u.switch_name for u in lookup.historic_usages] == ["sw-b"]

    async def test_other_tags_do_not_affect_the_verdict(self) -> None:
        vlans = [make_vlan(record_id=1, vlan_id=999)]
        assert (await build_service(vlans).lookup(1234, now=NOW)).is_available is True

    async def test_usage_carries_the_full_vlan_record(self) -> None:
        vlans = [
            make_vlan(
                record_id=1,
                vlan_id=1234,
                interfaces=(VlanInterface(name="ge-0/0/1", mode=InterfaceMode.TRUNK),),
            )
        ]
        usage = (await build_service(vlans).lookup(1234, now=NOW)).active_usages[0]
        assert usage.vlan.interface_count == 1
        assert usage.switch_site == "ADC NBO"


class TestReservedTags:
    @pytest.mark.parametrize("tag", [0, 4095])
    async def test_reserved_tags_are_not_reported_available(self, tag: int) -> None:
        """Reporting 0 or 4095 as 'available' would invite an engineer to assign
        one. 802.1Q reserves both."""
        lookup = await build_service([]).lookup(tag, now=NOW)
        assert lookup.availability is VlanAvailability.RESERVED
        assert lookup.is_available is False

    @pytest.mark.parametrize("tag", [-1, 4096, 99999])
    async def test_out_of_range_tags_are_rejected(self, tag: int) -> None:
        with pytest.raises(ValidationError):
            await build_service([]).lookup(tag, now=NOW)

    @pytest.mark.parametrize("tag", [1, 4094])
    async def test_boundary_tags_are_assignable(self, tag: int) -> None:
        lookup = await build_service([]).lookup(tag, now=NOW)
        assert lookup.availability is VlanAvailability.AVAILABLE


class TestStaleness:
    async def test_fresh_data_is_not_stale(self) -> None:
        lookup = await build_service([], last_finished=NOW - timedelta(minutes=5)).lookup(
            1234, now=NOW
        )
        assert lookup.is_stale is False
        assert lookup.data_as_of is not None

    async def test_old_data_is_stale(self) -> None:
        lookup = await build_service([], last_finished=NOW - timedelta(days=2)).lookup(
            1234, now=NOW
        )
        assert lookup.is_stale is True

    async def test_never_synced_counts_as_stale(self) -> None:
        """There is no evidence the data is current, so it must not read as fresh."""
        lookup = await build_service([], last_finished=None).lookup(1234, now=NOW)
        assert lookup.is_stale is True
        assert lookup.data_as_of is None

    async def test_threshold_boundary(self) -> None:
        service = build_service([], last_finished=NOW - timedelta(hours=6))
        assert (await service.lookup(1234, now=NOW)).is_stale is False

    async def test_reserved_verdict_is_never_stale(self) -> None:
        """It is a property of the standard, not of synced data."""
        lookup = await build_service([], last_finished=None).lookup(0, now=NOW)
        assert lookup.is_stale is False


class TestGetVlan:
    async def test_returns_the_record(self) -> None:
        service = build_service([make_vlan(record_id=42, vlan_id=100)])
        assert (await service.get_vlan(42)).vlan_id == 100

    async def test_unknown_id_raises_with_context(self) -> None:
        with pytest.raises(NotFoundError) as exc_info:
            await build_service([]).get_vlan(999)
        assert exc_info.value.details["vlan_record_id"] == 999


class TestSearch:
    async def test_filters_by_tag(self) -> None:
        vlans = [make_vlan(record_id=1, vlan_id=100), make_vlan(record_id=2, vlan_id=200)]
        page = await build_service(vlans).search(
            filters=VlanFilters(vlan_id=200), page_request=PageRequest()
        )
        assert [v.vlan_id for v in page.items] == [200]

    async def test_filters_by_state(self) -> None:
        vlans = [
            make_vlan(record_id=1, vlan_id=100),
            make_vlan(record_id=2, vlan_id=200, state=VlanState.MISSING),
        ]
        page = await build_service(vlans).search(
            filters=VlanFilters(state=VlanState.MISSING), page_request=PageRequest()
        )
        assert [v.vlan_id for v in page.items] == [200]

    async def test_search_matches_interface_name(self) -> None:
        vlans = [
            make_vlan(
                record_id=1,
                vlan_id=100,
                interfaces=(VlanInterface(name="ge-0/0/47"),),
            ),
            make_vlan(record_id=2, vlan_id=200),
        ]
        page = await build_service(vlans).search(
            filters=VlanFilters(search="ge-0/0/47"), page_request=PageRequest()
        )
        assert [v.vlan_id for v in page.items] == [100]

    async def test_pagination_reports_the_full_total(self) -> None:
        vlans = [make_vlan(record_id=i, vlan_id=100 + i) for i in range(1, 8)]
        page = await build_service(vlans).search(
            filters=VlanFilters(), page_request=PageRequest(page=2, page_size=3)
        )
        assert page.total == 7
        assert len(page.items) == 3
        assert page.total_pages == 3
